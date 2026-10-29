# lbprobe

**MPI 同步点等待测量探针** —— 只回答一个问题：程序跑起来的时候，进程之间到底**有没有在互相等**，**等了多少**，以及**是谁让别人等**。

![Language](https://img.shields.io/badge/C-C99-blue)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![Code size](https://img.shields.io/badge/core-~460%20lines-informational)
![Runs as](https://img.shields.io/badge/overhead-2%20clocks%20%2B%201%20line%20log-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 这是什么

动区域分解 + 负载均衡（负载再分配 / 网格拆域 / repartition）的 MPI 程序里，常见一种说不清的性能问题：

> 每个进程都在满负荷算，但总耗时比"最快的那个进程"多了很多 —— 时间花在哪了？

花在**等人**上。但这个判断需要证据，而且很容易搞错方向：

- "同步点多" ≠ "在等人"。集合通信本身有开销，那部分是通信该花的时间。
- "等待总量大" ≠ "负载不均"。也可能是启动阶段的一次性同步。
- "最后到达的进程只看次数" ≠ "它就慢"。曾有实测：某个 rank 只在 42% 的同步点上最后到达，
  看起来"分散"，但它那几次每次携带 2.5 秒的到达离散，**按等待量算占 99%** —— 典型的单点落后。

`lbprobe` 的做法是把"等待"这件事**定义清楚、拆到不重不漏、再归因到具体进程和具体调用点**。
插入成本是每个集合通信前后各一行，运行时开销是 2 次时钟读取 + 1 行文本写。

## 它怎么定义"等待"

每次同步（`Barrier` / `Allreduce` / `Alltoallv` / `Sendrecv` …）里：

- `in_r`、`out_r`：rank *r* 进入 / 退出该集合通信的时刻；
- `in_last = max(in_r)`：最后一个到齐的进程的时刻。

| 分段 | 定义 | 含义 |
|---|---|---|
| **到齐前等待** | `in_last − in_r` | 我算完了在等人。没有通信在为我服务，**这就是等待** |
| **到齐后耗时** | `out_r − in_last` | 全组到齐后原语自身的开销，计入通信成本，不算等待 |

两者之和**严格等于**该 rank 这次的等待时长 —— 报告里会自检这个恒等式，拆分不重不漏。

在此之上的聚合量与判定：

| 量 / 判定 | 含义 |
|---|---|
| 相互等待时长 | `Σ(各 rank 的到齐前等待)`，单位 rank·ms（可换算核·秒） |
| 平均同时在等待的 rank 数 | 等待区间并集的时间加权平均，即"平均有几个进程在干等" |
| **相互等待 vs 单向等待** | 等待**分散**在多个进程（最集中者 < 60%）→ 进度不一致，互相等；**几乎全归因于一个进程**（≥ 60%）→ 单点落后 |
| 等待热点 | 按同步类型与调用点（tag）分解，直接指向具体代码位置 |
| 等待责任归属 | 每次同步里谁最后到达，并按**等待量加权**统计（不是按次数） |

## 特性

- **极轻**：核心 `lbprobe.h` + `lbprobe.c` 约 460 行，**无线程、无锁、无第三方依赖、无对端文件轮询**；运行时只有每个 rank 一个只追加的文本日志。
- **两种接入布局**：常规两文件版，或头+实现合并的**单文件版**（`tools/make_single.py` 生成），适合"复制粘贴到集群上"。
- **一行插桩**：`LB_WRAP(LB_ALLREDUCE, MPI_Allreduce(...));` —— 自动用**该行行号**当 tag，报告里看到 `tag=1234` 就知道是哪个文件第 1234 行。不确定热点在哪时可以整片包上，让报告自己指出问题。
- **零开销开关**：定义 `LBPROBE_OFF` 后所有接口退化为空宏，可用于正式生产构建。
- **离线分析器只用 Python 标准库**：集群上 `python3` 直接能跑，不需要 pip 装任何东西。
- **自带仿真后端**：本机没有 MPI 也能跑通整条链路（编译 → 采集 → 分析 → 报告），CI 可复现。
- **能区分"有等待"和"相互等待"**，并把责任归到具体进程，而不是笼统报"有等待"。

## 快速开始

### 1. 本机验证整条链路（不需要 MPI）

```sh
make            # 编译仿真算例
make sim-all    # 跑 4 个合成算例并生成报告
```

跑完看 `runs/sim_realistic_*/report.md`。

### 2. 接入你自己的 MPI 项目

```c
#include "lbprobe.h"

int main(int argc, char **argv) {
    MPI_Init(&argc, &argv);
    lb_init();                        /* ① MPI_Init 之后一次 */

    for (iter = 0; ...; iter++) {
        /* 计算部分一行都不用改 */

        LB_WRAP(LB_ALLREDUCE,
                MPI_Allreduce(&load, &mx, 1, MPI_DOUBLE, MPI_MAX, MPI_COMM_WORLD));

        /* 或者手工两行，适用于非阻塞通信等任何原语 */
        lb_wait_begin(LB_ALLTOALLV, 901);
        MPI_Alltoallv(...);
        lb_wait_end(LB_ALLTOALLV, 901, 1);
    }

    lb_finalize();                    /* ② MPI_Finalize 之前一次 */
    MPI_Finalize();
}
```

```sh
mpicc -O2 -DLBPROBE_HAVE_MPI -Iinclude your_app.c src/lbprobe.c -o your_app

export LB_RUN_DIR=/dev/shm/lbprobe-$JOB_ID      # 必须放节点本地盘
export LB_COLLECT_DIR=$PWD/lbout
mkdir -p $LB_COLLECT_DIR
mpirun -np 8 ./your_app

python3 tools/lb_analyze.py $LB_COLLECT_DIR     # → report.md + analysis.json
```

完整的接入步骤（含怎么定位插桩点、编译报错排错表、Slurm/PBS 示例）见 [`docs/usage.md`](docs/usage.md)。

## 报告长什么样

`tools/lb_analyze.py` 生成中文 `report.md` 与机器可读 `analysis.json`：

```
0. 结论            是否存在相互等待 / 单向等待 + 等待时间 + 判定阈值
   关键量表        相互等待时长、同步等待合计、到齐前/到齐后占比、
                   平均同时在等待的 rank 数、等待占比、最大到达离散度
1. 等待热点        按同步类型（barrier / allreduce / alltoallv …）与调用点分解
2. 等待责任归属    每次同步的最后到达者 + 该 rank 自身等待占比
3. 逐同步点明细    每个同步点的到达离散、到齐前等待、到齐后耗时、最后到达者
4. 各 rank 等待分布 每个 rank 的等待时长及其占自身墙钟比例
5. 数据可靠性      非法日志行、未正常收尾、未配对的进入、调用次数不一致
```

仿真后端下 4 个合成算例的样例报告在 [`docs/samples/`](docs/samples/)，可直接读口径：

| 算例 | 判定 | 相互等待 (rank·ms) | 平均在等 (rank) | 热点 |
|---|---|---|---|---|
| [失衡负载](docs/samples/01-imbalanced-load-report.md) | **存在相互等待** | 8709.8 | 5.48 / 8 | allreduce 87.5% |
| [均匀负载](docs/samples/02-uniform-load-report.md) | 有等待，但主要不是进程间等待 | 856.4 | 4.05 / 8 | — |
| [末位 rank 长设备同步](docs/samples/03-single-rank-lagging-report.md) | **单向等待**（99% 归因 rank 7） | 141600.5 | 6.82 / 8 | allreduce 97.8% |
| [末位 rank 无分区](docs/samples/04-empty-rank-report.md) | **存在相互等待** | 7834.0 | 5.60 / 8 | allreduce 85.2% |

读法：**均匀负载那一行与失衡负载那一行的对比说明口径有效** —— 负载均匀时相互等待从 8709.8 降到 856.4
（约 1/10），说明判定没有把计时抖动当成失衡。**单向等待那一行说明责任归属有效** —— 它没有笼统报"有等待"，
而是指出 rank 7 在只有 41% 的同步点上最后到达、却按等待量占了 99%，判为单点落后。

## 真实案例

在一个 CFD 求解器上对比了两个版本：**A = 把网格域按网格量拆分给多进程的均衡化版本**，
**B = 按域个数轮流分配的默认版本**。同机、同资源、同算例、同预算下各跑多次：

| 指标 | A（均衡化） | B（默认分配） | 结论 |
|---|---|---|---|
| 单 GPU 最大网格负载 | **573 648** | 878 080 | A 低 **34.7%** |
| 60 秒内完成迭代 | **1300**（3/3 次一致） | 1210 | A **+7.4%** |
| 稳态等待 / 每迭代 | 0.687 rank·ms | 0.008 rank·ms | B 更少，见下 |
| 点对点 Wait 调用 | 201 182 次（均 0.001 ms） | 2 420 次 | A 多 83 倍但极便宜 |

关键读法：**B 的等待更少，是因为它没把负载摊开**——每个网格域整体归一个进程，
分组通信子只有自己一个成员，归约退化为本地空操作，**"不分享工作"自然"不用等"**。
A 多出来的等待是拆域并行的必要成本，摊到每进程 0.172 ms，**占每迭代时间的 0.37%**，
换来每迭代少 3.28 ms 墙钟 —— **收益/代价 ≈ 19 : 1**。

> 因此**不能用"等待总量"给两个版本排名**，那是拿"不并行"和"并行"比谁不用等。
> 完整方法与数据见 [`docs/case-study.md`](docs/case-study.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/method.md`](docs/method.md) | 测量口径、判定阈值、最小插桩集、集群执行注意事项 |
| [`docs/usage.md`](docs/usage.md) | 从零到出报告的 5 步接入指南 + 排错表 |
| [`docs/case-study.md`](docs/case-study.md) | 真实系统的均衡化 A/B 对照测量（方法与结论） |
| [`docs/samples/`](docs/samples/) | 仿真算例的完整样例报告 |

## 目录结构

```
include/lbprobe.h        探针接口（130 行）
src/lbprobe.c            探针实现（326 行）：时钟、事件日志、等待栈、包装函数
src/lbprobe_sim.c        仿真后端（无 MPI 时用假集合通信，仅供本机/CI 验证）
examples/lb_minimal_mpi.c  独立最小示例
tools/lb_analyze.py      离线分析器（仅标准库）→ report.md / analysis.json
tools/make_single.py     生成单文件版 lbprobe_single.h
tools/run_all.sh         集群批量跑法
tools/run_all_sim.sh     本机仿真跑法
tests/lb_scenarios.c     4 个合成算例（含单向等待、均匀负载等对照）
tests/single_check.c     单文件版自检
```

## 边界与已知限制

1. **只看 MPI 层**。GPU 核内等待、显存同步、I/O 阻塞不经过 MPI，探针看不到。
2. **点对点不套用"到齐"模型**。`MPI_Recv`/`Sendrecv`/`Wait` 没有"到齐"语义，
   报告里单独统计其**阻塞时长**（硬套集合通信模型会算出荒谬的数：实测把 8492 rank·ms 算成 1801 万）。
3. **跨节点比较绝对时刻依赖对时**（NTP/PTP）。对时不可靠时只看聚合量，别看单点绝对到达时刻。
4. **`LB_RUN_DIR` 必须放节点本地盘**（`/dev/shm`、`$TMPDIR`）。逐事件写盘落在 Lustre/GPFS/NFS
   上会拖慢并行文件系统，反过来干扰测量。
5. **长跑算例务必设 `LB_MAX_SYNC`**。每个同步点约 90 字节，百万级同步点会吃光内存盘；
   达到上限后停止采集并写 `truncated` 标记，报告会如实标注结论只覆盖采样窗口。
6. **只覆盖同步点**。纯计算阶段的负载不均不会直接显现，只会在下一次同步点上以等待的形式出现。

## 许可

[MIT](LICENSE)
