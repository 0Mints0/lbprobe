/* lbprobe.c - 轻量等待测量探针实现 (v2.0)
 *
 * 全部状态就这些: 一个文件句柄、一个跨进程可比的时钟、一个等待栈。
 * 没有线程, 没有锁, 没有对端文件, 没有 abort。
 *
 * 事件格式(每行一个 JSON, 实时落盘, 不需要正常退出也能拿到数据):
 *   {"r":0,"te":1789458742048.875,"it":0,"e":"init","nranks":8,"ver":"2.0.0"}
 *   {"r":0,"te":...,"it":0,"e":"enter","k":2,"tag":100}
 *   {"r":0,"te":...,"it":0,"e":"exit","k":2,"tag":100,"st":1,"dur_ms":12.34}
 *   {"r":0,"te":...,"it":0,"e":"finalize"}
 *
 * te = 绝对时钟(毫秒, 跨进程可比), 这是跨 rank 对齐时间线的唯一依据。
 */
#define _GNU_SOURCE
#include "lbprobe.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <time.h>

#if defined(LBPROBE_HAVE_MPI)
#  include <mpi.h>
#else
/* 仿真后端的内部接口(仅本机验证用, 定义在 lbprobe_sim.c) */
void lbsim_config(const char *dir, int rank, int nranks);
void lbsim_barrier(int tag);
double lbsim_allreduce(int tag, const char *key, double v, int is_max);
int lbsim_allgather_int(int tag, int v, int *out_all, int nranks);
int lbsim_bcast_int(int tag, int *v, int root, int nranks);
int lbsim_alltoallv(int tag, const int *sendcnt, const int *recvcnt, int nranks, int rank);
#endif

#if defined(_WIN32)
#  include <direct.h>
#  include <windows.h>
#  define LB_MKDIR(p) _mkdir(p)
#else
#  include <sys/stat.h>
#  include <sys/types.h>
#  define LB_MKDIR(p) mkdir((p), 0755)
#endif

/* ---------------- 时钟 ---------------- */
/* 绝对时钟: 跨进程/跨节点可比对, 用于把各 rank 的时间线对齐。
 * 依赖节点间对时(NTP/PTP)。若对时不可靠, 跨节点只看聚合量, 别看绝对时刻。 */
static double lb_now_ms(void)
{
#if defined(_WIN32)
    typedef void (WINAPI *pfn_t)(LPFILETIME);
    static pfn_t pfn = NULL;
    static int tried = 0;
    FILETIME ft;
    ULARGE_INTEGER u;
    if (!tried) {
        HMODULE k = GetModuleHandleA("kernel32.dll");
        tried = 1;
        if (k) pfn = (pfn_t)(void *)GetProcAddress(k, "GetSystemTimePreciseAsFileTime");
    }
    if (pfn) pfn(&ft); else GetSystemTimeAsFileTime(&ft);
    u.LowPart = ft.dwLowDateTime; u.HighPart = ft.dwHighDateTime;
    return (double)u.QuadPart / 10000.0 - 11644473600000.0;   /* 1601 -> 1970 */
#else
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1e6;
#endif
}

/* ---------------- 状态 ---------------- */
static struct {
    int    inited;
    int    enabled;
    int    rank, nranks;
    int    it;
    char   dir[512];
    FILE  *ev;
    /* 等待栈: 支持嵌套(例如均衡化里套了 Allreduce) */
    struct { int kind, tag; double t0; } stack[16];
    int    depth;
    int    overflow;              /* 栈满时未记时刻的进入次数 */
    double t_init;
} G;

/* 事件数上限(0 = 不限制)。长跑算例务必设置, 见 lb_wait_end 里的说明。 */
static long g_max_sync = 0;
static long g_nsync = 0;

static void lb_emit(const char *fmt, ...)
{
    va_list ap;
    if (!G.ev || !G.enabled) return;
    fprintf(G.ev, "{\"r\":%d,\"te\":%.3f,\"it\":%d,", G.rank, lb_now_ms(), G.it);
    va_start(ap, fmt);
    vfprintf(G.ev, fmt, ap);
    va_end(ap);
    fputs("}\n", G.ev);
    fflush(G.ev);            /* 实时落盘: 异常中止也要能拿到数据 */
}

static int lb_env_int(const char *k, int d)
{
    const char *s = getenv(k);
    return s ? atoi(s) : d;
}

int lb_init(void)
{
    const char *dir;
    char p[600];

    if (G.inited) return 0;
    memset(&G, 0, sizeof(G));
    G.enabled = 1;

    dir = getenv("LB_RUN_DIR");
    if (!dir || !dir[0]) dir = ".";
    snprintf(G.dir, sizeof(G.dir), "%s", dir);

#if defined(LBPROBE_HAVE_MPI)
    {
        int ini = 0;
        MPI_Initialized(&ini);
        if (ini) {
            MPI_Comm_rank(MPI_COMM_WORLD, &G.rank);
            MPI_Comm_size(MPI_COMM_WORLD, &G.nranks);
        } else if (lb_env_int("LB_MPI_AUTOINIT", 0)) {
            MPI_Init(NULL, NULL);
            MPI_Comm_rank(MPI_COMM_WORLD, &G.rank);
            MPI_Comm_size(MPI_COMM_WORLD, &G.nranks);
        } else {
            /* 探针不替应用决定 MPI 生命周期; 未初始化时退化为单进程记录 */
            G.rank = 0; G.nranks = 1;
        }
    }
#else
    G.rank   = lb_env_int("LB_RANK", 0);
    G.nranks = lb_env_int("LB_NRANKS", 1);
    lbsim_config(G.dir, G.rank, G.nranks);
#endif

    LB_MKDIR(G.dir);
    g_max_sync = lb_env_int("LB_MAX_SYNC", 0);
    snprintf(p, sizeof(p), "%s/events.rank%d.jsonl", G.dir, G.rank);
    G.ev = fopen(p, "wb");
    G.inited = 1;
    G.t_init = lb_now_ms();

    lb_emit("\"e\":\"init\",\"nranks\":%d,\"ver\":\"%s\",\"max_sync\":%ld,\"backend\":\"%s\"",
            G.nranks, LB_VERSION, g_max_sync,
#if defined(LBPROBE_HAVE_MPI)
            "mpi"
#else
            "sim"
#endif
            );
    return 0;
}

void lb_finalize(void)
{
    if (!G.inited) return;
    lb_emit("\"e\":\"finalize\",\"wall_ms\":%.3f", lb_now_ms() - G.t_init);
    if (G.ev) { fclose(G.ev); G.ev = NULL; }

    /* 集群上事件目录在节点本地盘, 这里一次性汇集到共享目录供离线分析 */
    {
        const char *dst = getenv("LB_COLLECT_DIR");
        if (dst && dst[0]) {
            char src[600], out[700], buf[8192];
            size_t n;
            FILE *fi, *fo;
            LB_MKDIR(dst);
            snprintf(src, sizeof(src), "%s/events.rank%d.jsonl", G.dir, G.rank);
            snprintf(out, sizeof(out), "%s/events.rank%d.jsonl", dst, G.rank);
            fi = fopen(src, "rb");
            if (fi) {
                fo = fopen(out, "wb");
                if (fo) {
                    while ((n = fread(buf, 1, sizeof(buf), fi)) > 0) fwrite(buf, 1, n, fo);
                    fclose(fo);
                }
                fclose(fi);
            }
        }
    }
    G.inited = 0;
}

int lb_rank(void)   { return G.rank; }
int lb_nranks(void) { return G.nranks; }

void lb_enable(int on) { G.enabled = on ? 1 : 0; }
int  lb_enabled(void)  { return G.enabled; }

/* ---------------- 核心: 进入/退出 ---------------- */
void lb_wait_begin(int kind, int tag)
{
    if (!G.inited) lb_init();
    if (!G.enabled) return;
    /* 栈满就只记日志不记时刻(绝不会错配到别人的时刻上) */
    if (G.depth < (int)(sizeof(G.stack) / sizeof(G.stack[0]))) {
        G.stack[G.depth].kind = kind;
        G.stack[G.depth].tag = tag;
        G.stack[G.depth].t0 = lb_now_ms();
        G.depth++;
    } else {
        G.overflow++;
    }
    lb_emit("\"e\":\"enter\",\"k\":%d,\"tag\":%d", kind, tag);
}

void lb_wait_end(int kind, int tag, int ok)
{
    double d = -1.0;
    if (!G.inited || !G.enabled) return;

    if (G.overflow > 0) {
        G.overflow--;                 /* 这次进入没有对应栈帧 */
    } else if (G.depth > 0) {
        G.depth--;
        d = lb_now_ms() - G.stack[G.depth].t0;   /* 用栈里的时刻, 避免日志 IO 抖动 */
    }
    /* d < 0 表示配对缺失(例如未成对的 enter), 分析器会跳过这类样本 */
    lb_emit("\"e\":\"exit\",\"k\":%d,\"tag\":%d,\"st\":%d,\"dur_ms\":%.4f",
            kind, tag, ok ? 1 : 0, d);

    /* 事件数上限保护: CFD 这类长跑算例的同步点可能达到百万级,
     * 日志写在内存盘(/dev/shm)上会吃光内存, 严重时反过来影响作业本身。
     * 达到上限即停止采集并留下明确标记, 让报告能说明"只覆盖采样窗口"。
     * 可用环境变量 LB_MAX_SYNC 设置(0 = 不限制, 默认)。 */
    if (g_max_sync > 0 && ++g_nsync >= g_max_sync) {
        lb_emit("\"e\":\"truncated\",\"nsync\":%ld,\"max\":%ld", g_nsync, g_max_sync);
        G.enabled = 0;
    }
}

/* ---------------- 便捷包装 ---------------- */
int lb_barrier(int tag)
{
    lb_wait_begin(LB_BARRIER, tag);
#if defined(LBPROBE_HAVE_MPI)
    MPI_Barrier(MPI_COMM_WORLD);
#else
    lbsim_barrier(tag);
#endif
    lb_wait_end(LB_BARRIER, tag, 1);
    return 1;
}

double lb_allreduce_max(int tag, double v)
{
    double out = v;
    lb_wait_begin(LB_ALLREDUCE, tag);
#if defined(LBPROBE_HAVE_MPI)
    MPI_Allreduce(&v, &out, 1, MPI_DOUBLE, MPI_MAX, MPI_COMM_WORLD);
#else
    out = lbsim_allreduce(tag, "redmax", v, 1);
#endif
    lb_wait_end(LB_ALLREDUCE, tag, 1);
    return out;
}

double lb_allreduce_sum(int tag, double v)
{
    double out = v;
    lb_wait_begin(LB_ALLREDUCE, tag);
#if defined(LBPROBE_HAVE_MPI)
    MPI_Allreduce(&v, &out, 1, MPI_DOUBLE, MPI_SUM, MPI_COMM_WORLD);
#else
    out = lbsim_allreduce(tag, "redsum", v, 0);
#endif
    lb_wait_end(LB_ALLREDUCE, tag, 1);
    return out;
}

int lb_allgather_int(int tag, int v, int *out_all)
{
    int ok = 1;
    lb_wait_begin(LB_ALLGATHER, tag);
#if defined(LBPROBE_HAVE_MPI)
    MPI_Allgather(&v, 1, MPI_INT, out_all, 1, MPI_INT, MPI_COMM_WORLD);
#else
    ok = lbsim_allgather_int(tag, v, out_all, G.nranks);
#endif
    lb_wait_end(LB_ALLGATHER, tag, ok);
    return ok;
}

int lb_bcast_int(int tag, int *v, int root)
{
    lb_wait_begin(LB_BCAST, tag);
#if defined(LBPROBE_HAVE_MPI)
    MPI_Bcast(v, 1, MPI_INT, root, MPI_COMM_WORLD);
#else
    lbsim_bcast_int(tag, v, root, G.nranks);
#endif
    lb_wait_end(LB_BCAST, tag, 1);
    return 1;
}

int lb_alltoallv(int tag, const int *sendcnt, const int *recvcnt)
{
    int ok = 1;
    lb_wait_begin(LB_ALLTOALLV, tag);
#if defined(LBPROBE_HAVE_MPI)
    {
        int i, ssum = 0, rsum = 0;
        int *sd = (int *)malloc(sizeof(int) * (size_t)G.nranks);
        int *rd = (int *)malloc(sizeof(int) * (size_t)G.nranks);
        char *sb, *rb;
        for (i = 0; i < G.nranks; i++) { sd[i] = ssum; ssum += (sendcnt[i] > 0 ? sendcnt[i] : 0); }
        for (i = 0; i < G.nranks; i++) { rd[i] = rsum; rsum += (recvcnt[i] > 0 ? recvcnt[i] : 0); }
        sb = (char *)calloc((size_t)(ssum > 0 ? ssum : 1), 1);
        rb = (char *)calloc((size_t)(rsum > 0 ? rsum : 1), 1);
        MPI_Alltoallv(sb, (int *)sendcnt, sd, MPI_CHAR,
                      rb, (int *)recvcnt, rd, MPI_CHAR, MPI_COMM_WORLD);
        free(sb); free(rb); free(sd); free(rd);
    }
#else
    ok = lbsim_alltoallv(tag, sendcnt, recvcnt, G.nranks, G.rank);
#endif
    lb_wait_end(LB_ALLTOALLV, tag, ok);
    return ok;
}
