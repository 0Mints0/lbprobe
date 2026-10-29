/* lbprobe_sim.c - 仿真后端 (仅用于本机/CI 验证, 不要插进你们的项目)
 *
 * 作用: 在没有 MPI 的机器上, 用共享目录里的文件模拟集合通信, 让测量链路
 *       (探针 -> 事件日志 -> 分析器 -> 报告) 能被完整跑通和自检。
 *
 * 真集群上编译时加 -DLBPROBE_HAVE_MPI, 这个文件根本不参与编译。
 *
 * 实现: 每个参与者在自己的 slot 上落一个文件, 然后等所有 slot 出现。
 *       等待带本地超时兜底, 超时只返回失败、不中断进程, 便于留下证据。
 */
#define _GNU_SOURCE
#include "lbprobe.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#if defined(_WIN32)
#  include <windows.h>
#else
#  include <unistd.h>
#endif

static struct {
    char   dir[512];
    int    rank, nranks;
    double timeout_ms;
} S = { ".", 0, 1, 10000.0 };

/* 单调时钟: 超时判定用, 不受系统时间调整影响 */
static double sim_mono_ms(void)
{
#if defined(_WIN32)
    LARGE_INTEGER f, c;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart * 1000.0 / (double)f.QuadPart;
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1e6;
#endif
}

static void sim_sleep_ms(double ms)
{
#if defined(_WIN32)
    Sleep((DWORD)(ms + 0.5));
#else
    usleep((useconds_t)(ms * 1000.0 + 0.5));
#endif
}

void lbsim_config(const char *dir, int rank, int nranks)
{
    const char *t = getenv("LB_WAIT_TIMEOUT_MS");
    snprintf(S.dir, sizeof(S.dir), "%s", dir ? dir : ".");
    S.rank = rank;
    S.nranks = nranks > 0 ? nranks : 1;
    S.timeout_ms = t ? atof(t) : 10000.0;
}

/* 生成 "<dir>/<name>" */
static void sim_path(char *out, size_t n, const char *fmt, ...)
{
    char name[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(name, sizeof(name), fmt, ap);
    va_end(ap);
    snprintf(out, n, "%s/%s", S.dir, name);
}

static void sim_touch(const char *fmt, ...)
{
    char name[256], path[700];
    va_list ap;
    FILE *f;
    va_start(ap, fmt);
    vsnprintf(name, sizeof(name), fmt, ap);
    va_end(ap);
    snprintf(path, sizeof(path), "%s/%s", S.dir, name);
    f = fopen(path, "wb");
    if (f) { fputc('1', f); fclose(f); }
}

static int sim_exists(const char *path)
{
    FILE *f = fopen(path, "rb");
    if (f) { fclose(f); return 1; }
    return 0;
}

/* 轮询直到 "所有 key.<tag>.<r> 都出现" 或超时; 返回 1 表示到齐 */
static int sim_wait_all(const char *key, int tag)
{
    char path[700];
    double deadline = sim_mono_ms() + S.timeout_ms;
    for (;;) {
        int i, all = 1;
        for (i = 0; i < S.nranks; i++) {
            sim_path(path, sizeof(path), "%s.%d.%d", key, tag, i);
            if (!sim_exists(path)) { all = 0; break; }
        }
        if (all) return 1;
        if (sim_mono_ms() > deadline) return 0;
        sim_sleep_ms(1.0);
    }
}

void lbsim_barrier(int tag)
{
    sim_touch("bar.%d.%d", tag, S.rank);
    sim_wait_all("bar", tag);
}

double lbsim_allreduce(int tag, const char *key, double v, int is_max)
{
    char path[700];
    int i;
    double acc = is_max ? -1e308 : 0.0;

    sim_path(path, sizeof(path), "%s.%d.%d", key, tag, S.rank);
    {
        FILE *f = fopen(path, "wb");
        if (f) { fprintf(f, "%.17g", v); fclose(f); }
    }
    if (!sim_wait_all(key, tag)) return v;
    for (i = 0; i < S.nranks; i++) {
        double x = 0.0;
        sim_path(path, sizeof(path), "%s.%d.%d", key, tag, i);
        {
            FILE *f = fopen(path, "rb");
            if (f) { if (fscanf(f, "%lf", &x) != 1) x = 0.0; fclose(f); }
        }
        acc = is_max ? (x > acc ? x : acc) : (acc + x);
    }
    return acc;
}

int lbsim_allgather_int(int tag, int v, int *out_all, int nranks)
{
    char path[700];
    int i;
    sim_path(path, sizeof(path), "ag.%d.%d", tag, S.rank);
    {
        FILE *f = fopen(path, "wb");
        if (f) { fprintf(f, "%d", v); fclose(f); }
    }
    if (!sim_wait_all("ag", tag)) return 0;
    for (i = 0; i < nranks; i++) {
        out_all[i] = 0;
        sim_path(path, sizeof(path), "ag.%d.%d", tag, i);
        {
            FILE *f = fopen(path, "rb");
            if (f) { if (fscanf(f, "%d", &out_all[i]) != 1) out_all[i] = 0; fclose(f); }
        }
    }
    return 1;
}

int lbsim_bcast_int(int tag, int *v, int root, int nranks)
{
    char path[700];
    double deadline;
    (void)nranks;
    if (S.rank == root) {
        sim_path(path, sizeof(path), "bc.%d", tag);
        {
            FILE *f = fopen(path, "wb");
            if (f) { fprintf(f, "%d", v ? *v : 0); fclose(f); }
        }
        return 1;
    }
    sim_path(path, sizeof(path), "bc.%d", tag);
    deadline = sim_mono_ms() + S.timeout_ms;
    for (;;) {
        FILE *f = fopen(path, "rb");
        if (f) {
            if (fscanf(f, "%d", v) != 1) { /* 内容异常就保留原值 */ }
            fclose(f);
            return 1;
        }
        if (sim_mono_ms() > deadline) return 0;
        sim_sleep_ms(1.0);
    }
}

/* 每个 rank 落自己的发送 slot, 再等所有应收对端的 slot */
int lbsim_alltoallv(int tag, const int *sendcnt, const int *recvcnt, int nranks, int rank)
{
    char path[700];
    double deadline;
    int i;
    for (i = 0; i < nranks; i++) {
        if (sendcnt && sendcnt[i] < 0) continue;      /* 负值 = 该发而未发 */
        sim_touch("a2a.%d.%d.%d", tag, rank, i);
    }
    deadline = sim_mono_ms() + S.timeout_ms;
    for (;;) {
        int all = 1;
        for (i = 0; i < nranks; i++) {
            if (recvcnt && recvcnt[i] < 0) continue;
            sim_path(path, sizeof(path), "a2a.%d.%d.%d", tag, i, rank);
            if (!sim_exists(path)) { all = 0; break; }
        }
        if (all) return 1;
        if (sim_mono_ms() > deadline) return 0;
        sim_sleep_ms(1.0);
    }
}
