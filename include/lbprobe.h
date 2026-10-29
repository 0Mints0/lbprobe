/* lbprobe.h - 同步点等待测量探针 (轻量版 v2.0)
 *
 * 只做一件事: 测出每次集合通信里"等了多久", 用来回答
 *   「当前计算过程中, 进程之间是否存在相互等待, 等待时间是多少」。
 *
 * 设计取向: 插桩越轻越好。
 *   - 没有后台线程、没有心跳文件、没有看门狗、不轮询对端;
 *   - 只有每个 rank 一个只追加的文本日志(一行一事件), 实时落盘;
 *   - 全部结论由这些行里的时刻算出来: 等待时长 = 退出时刻 - 进入时刻;
 *     「到齐前等待」= 最晚进入者时刻 - 本 rank 进入时刻。
 *
 * 最小插法(每个集合通信前后各一行, 两种方式任选):
 *   A) 手工插两行 —— 适用于任何 MPI 调用(含非阻塞、含 Bcast 等):
 *        lb_wait_begin(LB_ALLREDUCE, tag);
 *        MPI_Allreduce(...);
 *        lb_wait_end(LB_ALLREDUCE, tag, 1);
 *   B) 直接用包装函数替换调用(仅覆盖常用几个原语):
 *        double mx = lb_allreduce_max(tag, local_load);
 *
 * 编译:
 *   真 MPI :  mpicc -DLBPROBE_HAVE_MPI -Iinclude your_code.c src/lbprobe.c
 *   本机验证: gcc -Iinclude ... src/lbprobe.c src/lbprobe_sim.c   (无需 MPI)
 *   关闭探针: 定义 LBPROBE_OFF 后所有接口退化为空操作, 零开销
 */
#ifndef LBPROBE_H
#define LBPROBE_H

#ifdef __cplusplus
extern "C" {
#endif

#define LB_VERSION "2.0.0"

/* 同步类型: 用于把等待归因到具体的集合通信 */
enum {
    LB_OTHER = 0,
    LB_BARRIER,
    LB_ALLREDUCE,
    LB_ALLGATHER,
    LB_ALLTOALLV,
    LB_SENDRECV,
    LB_RECV,
    LB_BCAST
};

#ifndef LBPROBE_OFF

/* ---- 生命周期 -------------------------------------------------------- */
/* 返回 0 成功。目录取环境变量 LB_RUN_DIR(缺省当前目录)。
 * MPI 后端要求调用前已 MPI_Init; rank/nranks 自动从 MPI_COMM_WORLD 取。
 * 注意: LB_RUN_DIR 请放在**节点本地盘**(/dev/shm 或 $TMPDIR), 不要放
 *       Lustre/GPFS/NFS 家目录 —— 频繁小 IO 会拖慢并行文件系统, 干扰测量。
 *
 * 相关环境变量:
 *   LB_RUN_DIR        事件日志目录(建议 /dev/shm/...), 缺省当前目录
 *   LB_COLLECT_DIR    可选, lb_finalize() 时把日志汇集到此共享目录
 *   LB_MAX_SYNC       可选, 同步点数量上限(0=不限制)。**长跑算例强烈建议设置**:
 *                     每个同步点约 90 字节写进内存盘, 百万级同步点会吃光内存。
 *                     达到上限后停止采集并写入 truncated 标记, 报告会如实标注
 *                     结论只覆盖采样窗口。例: LB_MAX_SYNC=20000 */
int  lb_init(void);
void lb_finalize(void);
int  lb_rank(void);
int  lb_nranks(void);

/* ---- 核心: 集合通信前后各一行 ---------------------------------------- */
void lb_wait_begin(int kind, int tag);
void lb_wait_end(int kind, int tag, int ok);   /* ok: 正常返回传 1 */

/* ---- 便捷包装(可选, 只覆盖常用原语) ---------------------------------- */
int    lb_barrier(int tag);
double lb_allreduce_max(int tag, double v);
double lb_allreduce_sum(int tag, double v);
int    lb_allgather_int(int tag, int v, int *out_all);
int    lb_bcast_int(int tag, int *v, int root);
int    lb_alltoallv(int tag, const int *sendcnt, const int *recvcnt);

/* ---- 一行插桩宏(推荐, 省事且不会填错 tag) ----------------------------
 * 把整个调用包进去, 自动用**该行行号**当 tag:
 *     LB_WRAP(LB_ALLREDUCE,
 *             MPI_Allreduce(&load, &mx, 1, MPI_DOUBLE, MPI_MAX, MPI_COMM_WORLD));
 *
 * 好处: 每个调用点只加一行; 不用手编 tag; tag 与源码行号一一对应
 *       (报告里看到 tag=1234, 就知道是那个文件第 1234 行)。
 *       所以当你不确定到底哪一处是热点时, 可以放心地把一大片集合通信都包上,
 *       让报告的"等待热点"自己把问题指出来 —— 每处只多一行, 开销可忽略。
 * 限制: 只包**单个调用语句**; 不要嵌套使用; 非阻塞通信请改用手工两行
 *       (begin 在发起前、end 在 Wait 之后)。
 * 重要: 只能包**裸 MPI 调用**。下面的 lb_* 包装函数自身已经插过桩了,
 *       再用 LB_WRAP 包一层会把同一次同步记两遍(报告里等待翻倍):
 *         LB_WRAP(LB_ALLREDUCE, MPI_Allreduce(...));   ✅
 *         LB_WRAP(LB_BARRIER,    lb_barrier(100));     ❌ 重复插桩
 */
#define LB_WRAP(kind, stmt)                     \
    do {                                        \
        const int lbt_ = __LINE__;              \
        lb_wait_begin((kind), lbt_);            \
        stmt;                                   \
        lb_wait_end((kind), lbt_, 1);           \
    } while (0)

/* ---- 采集开关(可选) -------------------------------------------------- */
/* 只测量中间一段: 前处理/前期计算里只关心均衡化相关的那几轮时可用 */
void lb_enable(int on);
int  lb_enabled(void);

#else  /* LBPROBE_OFF: 全部退化为空操作 */

#define lb_init()                     (0)
#define lb_finalize()                 ((void)0)
#define lb_rank()                     (0)
#define lb_nranks()                   (1)
#define lb_wait_begin(k, t)           ((void)0)
#define lb_wait_end(k, t, ok)         ((void)0)
#define lb_barrier(t)                 (1)
#define lb_allreduce_max(t, v)        (v)
#define lb_allreduce_sum(t, v)        (v)
#define lb_allgather_int(t, v, o)     (1)
#define lb_bcast_int(t, v, r)         (1)
#define lb_alltoallv(t, s, r)         (1)
#define LB_WRAP(kind, stmt)           do { stmt; } while (0)
#define lb_enable(on)                 ((void)0)
#define lb_enabled()                  (0)

#endif /* LBPROBE_OFF */

#ifdef __cplusplus
}
#endif
#endif /* LBPROBE_H */
