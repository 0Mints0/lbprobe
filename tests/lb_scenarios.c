/* lb_scenarios.c - 仿真算例（仅用于本机验证测量链路, 不是交付给项目的代码）
 *
 * 用法: lb_scenarios <scenario> [iters]
 *   rank/nranks 由 LB_RANK / LB_NRANKS 提供(mpirun 或 tools/simrun.py 设置)
 *
 * 四个算例都是**单次测量**, 不做任何开/关对照:
 *   sim_realistic  失衡负载 + 均衡化 —— 主要测量形态
 *   sim_uniform    均匀负载 + 均衡化 —— 口径对照(等待应由原语开销主导)
 *   sim_gpu_sync   末位 rank 长 GPU 同步 —— 验证"等设备"会被正确归因到该 rank
 *   sim_empty_rank 末位 rank 无分区   —— 空 rank 空等是否被如实计量
 *
 * 插桩方式就是方案里推荐的"最小集": 只在集合通信前后各一行。
 */
#include "lbprobe.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if defined(_WIN32)
#  include <windows.h>
static void burn(double ms) { if (ms > 0) Sleep((DWORD)(ms + 0.5)); }
#else
#  include <unistd.h>
static void burn(double ms) { if (ms > 0) usleep((useconds_t)(ms * 1000.0 + 0.5)); }
#endif

static int    N, rank, iters;
static double BASE = 30.0;      /* 基础计算量 (ms) */
static double EXTRA = 150.0;    /* 热点额外计算量 (ms) */
static double MIG = 6.0;        /* 均衡化里数据迁移的开销 (ms) */
static int    ZERO_RANK = -1;   /* 该 rank 分区数为 0 */

/* 本 rank 在第 it 轮的计算量: 热点随迭代轮转, 模拟负载漂移 */
static double load_of(int it)
{
    int hot;
    if (ZERO_RANK >= 0 && rank == ZERO_RANK) return 0.0;
    hot = (iters > 0) ? (it % N) : (N - 1);
    return BASE + (rank == hot ? EXTRA : 0.0);
}

/* 被测量的"均衡化"步骤: 全局统计 + 判据 + 迁移。
 * 每个集合通信都用最小插桩(lb_wait_begin/lb_wait_end 或包装函数)。 */
static void balance_step(int tag, double my_load)
{
    int i;
    int *send, *recv;
    double mx, sum;

    mx  = lb_allreduce_max(tag, my_load);              /* 全局最大负载 */
    sum = lb_allreduce_sum(tag + 1, my_load);          /* 全局总负载 */

    if (mx > (sum / (double)N) * 1.05) {               /* 需要重划分 */
        send = (int *)malloc(sizeof(int) * (size_t)N);
        recv = (int *)malloc(sizeof(int) * (size_t)N);
        for (i = 0; i < N; i++) { send[i] = 1; recv[i] = 1; }
        lb_alltoallv(tag + 2, send, recv);             /* 分区/数据迁移 */
        free(send); free(recv);
        burn(MIG);
    }
}

int main(int argc, char **argv)
{
    const char *scen = (argc > 1) ? argv[1] : "sim_realistic";
    const char *e;
    int it;

    iters = (argc > 2) ? atoi(argv[2]) : 6;
    if ((e = getenv("LB_BASE_MS"))  != NULL) BASE  = atof(e);
    if ((e = getenv("LB_EXTRA_MS")) != NULL) EXTRA = atof(e);

    lb_init();
    N    = lb_nranks();
    rank = lb_rank();
    if (N <= 0) N = 1;

    if (!strcmp(scen, "sim_uniform")) {
        EXTRA = 0.0;                       /* 无失衡 */
    } else if (!strcmp(scen, "sim_gpu_sync")) {
        /* 末位 rank 每轮做一次很长的设备同步, 其余 rank 在 barrier 等它 */
    } else if (!strcmp(scen, "sim_empty_rank")) {
        ZERO_RANK = N - 1;                 /* 末位 rank 分区数为 0 */
    }

    for (it = 0; it < iters; it++) {
        double load = load_of(it);

        /* --- 计算(不插桩: 只测同步点上的等待) --- */
        if (load > 0.0) {
            burn(load);
        } else {
            /* 空分区 rank: 没有计算 */
        }

        if (!strcmp(scen, "sim_gpu_sync") && rank == N - 1) {
            /* 长设备同步: 注意这里不在任何集合通信内,
             * 所以它不会被记成"进程间互相等待"; 其余 rank 在 barrier 等它,
             * 报告里应把最后到达者归因到这个 rank。 */
            burn(2500.0);
        }

        /* --- 均衡化(内部含集合通信, 用最小插桩) --- */
        balance_step(1000 + it * 10, load);

        /* --- 每轮结束的全局同步 --- */
        lb_barrier(2000 + it);
    }

    lb_finalize();
    return 0;
}
