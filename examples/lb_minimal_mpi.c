/* lb_minimal_mpi.c - 最小可跑示例 (真 MPI)
 *
 * 用途: 在动你们项目代码之前, 先用这个独立小程序在集群上把整条链路跑通:
 *         编译 -> 运行 -> 产生事件日志 -> 分析出报告
 *       跑通了再往自己的代码里插, 可以避免把"工具没配好"和"代码有问题"混在一起查。
 *
 * 它模拟你们的情况: 各 rank 计算量不同(末尾 rank 是热点), 每轮做一次全局统计
 * 和一次全局同步, 都用探针插桩。
 *
 * 编译(集群):
 *   mpicc -O2 -DLBPROBE_HAVE_MPI -I<lbprobe目录> lb_minimal_mpi.c <lbprobe目录>/src/lbprobe.c -o lb_minimal
 * 单文件版的话:
 *   mpicc -O2 -DLBPROBE_HAVE_MPI -I<lbprobe目录> -DLBPROBE_IMPLEMENTATION lb_minimal_mpi.c -o lb_minimal
 *
 * 运行:
 *   export LB_RUN_DIR=/dev/shm/lbprobe-demo
 *   export LB_COLLECT_DIR=$PWD/lbprobe_out && mkdir -p $LB_COLLECT_DIR
 *   mpirun -np 8 ./lb_minimal 10
 *
 * 分析:
 *   python3 tools/lb_analyze.py $LB_COLLECT_DIR
 *   cat $LB_COLLECT_DIR/report.md
 */
#include <mpi.h>
#include <stdio.h>
#include <stdlib.h>

#include "lbprobe.h"

#if defined(_WIN32)
#  include <windows.h>
static void work_ms(double ms) { if (ms > 0) Sleep((DWORD)(ms + 0.5)); }
#else
#  include <unistd.h>
static void work_ms(double ms) { if (ms > 0) usleep((useconds_t)(ms * 1000.0 + 0.5)); }
#endif

int main(int argc, char **argv)
{
    int rank, nranks, it, iters = (argc > 1) ? atoi(argv[1]) : 10;
    double base = 20.0, extra = 60.0;      /* 基础/热点额外计算量 (ms) */

    MPI_Init(&argc, &argv);
    MPI_Comm_rank(MPI_COMM_WORLD, &rank);
    MPI_Comm_size(MPI_COMM_WORLD, &nranks);

    if (lb_init() != 0) { fprintf(stderr, "lb_init failed\n"); MPI_Abort(MPI_COMM_WORLD, 1); }

    for (it = 0; it < iters; it++) {
        int    hot = it % nranks;                     /* 热点逐轮移动 */
        double my_work = base + (rank == hot ? extra : 0.0);
        double my_load = my_work, mx, sum;

        /* ---- 计算部分: 不插桩 ---- */
        work_ms(my_work);

        /* ---- 均衡化里的全局统计: 前后各一行插桩 ---- */
        lb_wait_begin(LB_ALLREDUCE, 900);
        MPI_Allreduce(&my_load, &mx, 1, MPI_DOUBLE, MPI_MAX, MPI_COMM_WORLD);
        lb_wait_end(LB_ALLREDUCE, 900, 1);

        lb_wait_begin(LB_ALLREDUCE, 901);
        MPI_Allreduce(&my_load, &sum, 1, MPI_DOUBLE, MPI_SUM, MPI_COMM_WORLD);
        lb_wait_end(LB_ALLREDUCE, 901, 1);

        /* ---- 每轮结束的全局同步: 前后各一行插桩 ---- */
        lb_wait_begin(LB_BARRIER, 950);
        MPI_Barrier(MPI_COMM_WORLD);
        lb_wait_end(LB_BARRIER, 950, 1);

        if (rank == 0 && (mx > 0.0) && (sum > 0.0)) { /* 防止被优化掉, 无实际含义 */ }
    }

    lb_finalize();
    if (rank == 0) printf("done: %d iters on %d ranks\n", iters, nranks);
    MPI_Finalize();
    return 0;
}
