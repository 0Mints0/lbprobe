# 接入指南：把探针用到你的项目里

面向 C/C++ + MPI 项目，从零到出报告的 5 步。全程不改动计算逻辑，只在集合通信前后各加一行。

---

## 0. 用哪几个文件

| 文件 | 用途 | 要不要放进你的项目 |
|---|---|---|
| `lbprobe_single.h` | **单文件版**（头+实现，490 行） | ✅ 想最省事就用这个 |
| `lbprobe.h` + `lbprobe.c` | 两文件版（130 + 326 行） | ✅ 或者用这种常规布局 |
| `lb_analyze.py` | 离线分析，生成报告 | ✅ 放到能跑 python3 的地方 |
| `lb_minimal_mpi.c` | 独立最小示例 | 建议先拿它在集群上跑通 |
| `lbprobe_sim.c`、`lb_scenarios.c`、`run_all*.sh`、`simrun.py` 等 | 本机仿真验证用 | ❌ **不要**放进项目 |

> `lbprobe_single.h` 不入库（它是生成物），用 `python3 tools/make_single.py` 从
> `include/lbprobe.h` + `src/lbprobe.c` 生成：
>
> ```sh
> python3 tools/make_single.py     # → lbprobe_single.h（490 行）
> ```


> 上表用的是本仓库的路径写法（`include/`、`src/`、`tools/`、`examples/` 是子目录）。
> **手工复制粘贴时通常是平铺的**，那就只用文件名即可——编译命令也随之不同，见 §2。

两种布局二选一，**不要同时用**（会重复定义符号）。

---

## 1. 把文件弄到集群上

三个途径，选一个：

**A. 直接复制粘贴（最省事，推荐）**
单文件版只有 490 行，`cat lbprobe_single.h` 全部贴到集群上的编辑器即可；分析脚本同理。不需要任何传输工具。

**B. rsync / scp**
```sh
# 在能访问集群的机器上
scp -r lbprobe  user@集群:~/lbprobe
# 或在集群上反向拉
rsync -av <这台机器>:<工作区路径>/lbprobe/ ~/lbprobe/
```

**C. 只传必需的三样**
```sh
~/lbprobe/lbprobe_single.h      # 或 include/lbprobe.h + src/lbprobe.c
~/lbprobe/tools/lb_analyze.py
```

> 分析脚本只用 Python 标准库，**不需要 pip 装任何东西**，集群上的 python3 直接能跑。

---

## 2. 先在集群上跑通独立示例（强烈建议，约 1 分钟）

**先别动你的代码。** 用独立示例把链路验证一遍，这样可以避免后面把"工具没配好"和"代码有问题"混在一起查。

**先确认文件放在哪了**——下面的编译命令依赖你的实际布局：

```sh
pwd; ls -la
find . -maxdepth 3 \( -name 'lbprobe*' -o -name 'lb_minimal*' \) -print
```

**情况 A：文件平铺在同一个目录**（手工复制粘贴时最常见）

```sh
cd <那三个文件所在的目录>
mpicc -O2 -DLBPROBE_HAVE_MPI -I. lb_minimal_mpi.c lbprobe.c -o lb_minimal
```

**情况 B：保持了本仓库的子目录结构**（`include/` `src/` `examples/`）

```sh
cd ~/lbprobe
mpicc -O2 -DLBPROBE_HAVE_MPI -Iinclude examples/lb_minimal_mpi.c src/lbprobe.c -o lb_minimal
```

**情况 C：你手上是单文件版 `lbprobe_single.h`**

示例里 `#include` 的是 `lbprobe.h`，所以要么改名，要么把示例里的 include 改掉。
并且要给实现宏，否则链接时报一堆 undefined reference：

```sh
cp lbprobe_single.h lbprobe.h     # 让示例的 #include "lbprobe.h" 能找到它
mpicc -O2 -DLBPROBE_HAVE_MPI -DLBPROBE_IMPLEMENTATION -I. lb_minimal_mpi.c -o lb_minimal
```

> **不要把 `lbprobe.c` 和单文件版一起加进命令行**，会重复定义符号。
> 多文件项目里 `LBPROBE_IMPLEMENTATION` 只能出现在一个编译单元（见 §5）。

> **严格 C99 模式下的一个坑**：探针需要一个亚毫秒时钟（`clock_gettime`），它属于 POSIX 而不是 C99 标准。单文件版已经把这个特性宏放在文件最顶部，但如果你的 TU 先 include 了 `mpi.h` 等系统头，特性宏就来不及生效。此时若编译选项是严格的 `-std=c99` / `-std=c11`，会报 `implicit declaration of function 'clock_gettime'`。二选一处理：
> 1. 把 `#include "lbprobe.h"` 放到该文件的**第一行**；或
> 2. 编译时加 `-D_POSIX_C_SOURCE=200809L`（或 `-D_GNU_SOURCE`）。
>
> 用默认 gnu 模式（不加 `-std=`）或用 C++ 编译（g++ 自带 `_GNU_SOURCE`）则无需处理。
> **两文件版不受影响**：`lbprobe.c` 自身第一行就定义了特性宏，作为独立编译单元永远安全。

**两种都适用的两个要点**

1. `-I<目录>` 只告诉编译器**去哪找 `#include` 的头文件**；`.c` 文件必须给**真实路径**，不能用 `-I` 代替。所以平铺布局要用 `-I.`，而不是 `-Iinclude`。
2. 编译完确认可执行文件真的生成了，否则后面 `mpirun` 只会报"could not access or execute"（那是在告诉你文件不存在，不是 MPI 的问题）：

```sh
ls -l lb_minimal
wc -l lbprobe.h lbprobe.c lb_minimal_mpi.c
# 行数指纹应为: 130 / 326 / 78；差很多说明复制被截断了
head -2 lbprobe.h      # 应显示: /* lbprobe.h - 同步点等待测量探针 (轻量版 v2.0)
```

然后运行：

```sh
export LB_RUN_DIR=/dev/shm/lbprobe-demo
export LB_COLLECT_DIR=$PWD/lbprobe_out
mkdir -p $LB_COLLECT_DIR
mpirun -np 8 ./lb_minimal 20

ls $LB_COLLECT_DIR                     # 应看到 events.rank0..7.jsonl
python3 <分析脚本所在路径>/lb_analyze.py $LB_COLLECT_DIR
head -40 $LB_COLLECT_DIR/report.md     # 出报告即链路通了
```

看到报告就成了。如果这一步不通，先看 §7 排错表，**不要**继续往下做。

---

## 3. 找到插桩点

### 3.1 最可靠的办法：直接问 git

均衡化是一批新加入的代码，**git 就是权威答案**，比任何猜测都准：

```sh
git log --oneline -20                     # 找到加入均衡化的那次提交
git diff <那次提交之前>..HEAD --stat        # 看动了哪些文件
# 关键: 直接把新增的集合通信调用列出来 —— 这就是第一批插桩点, 不多不少
git diff <那次提交之前>..HEAD \
  | grep -nE '^\+.*MPI_(Allreduce|Alltoallv|Alltoall|Allgatherv?|Barrier|Bcast|Reduce|Sendrecv)'
```

如果均衡化分散在多次提交里，就把区间放大（例如 `git diff v1.0..HEAD`），或按作者过滤：

```sh
git log --oneline --author=<作者> -20
```

### 3.2 没有 git 时的筛选办法

**按函数名找**：`balance` / `rebalance` / `repartition` / `redistribute` / `migrate` / `adapt` / `decomp`

**按原语特征找**（均衡化的特征很鲜明）：

| 特征 | 说明 |
|---|---|
| `MPI_Alltoallv` / `MPI_Alltoall` | 数据/分区迁移几乎必须用它 |
| `MPI_Allreduce` 且操作为 `MPI_MAX` / `MPI_SUM` | 全局最大/总负载统计 |
| `MPI_Allgather` | 收集各 rank 的负载向量 |

**兜底：列出全部调用点人工看**

```sh
grep -rn --include=*.c --include=*.h --include=*.cpp --include=*.hpp --include=*.cu \
  -E 'MPI_(Allreduce|Alltoallv|Alltoall|Allgatherv?|Barrier|Bcast|Reduce|Scatterv?|Gatherv?|Sendrecv|Irecv|Isend|Send|Recv|Waitall|Wait)\s*\(' \
  src/ include/ 2>/dev/null | grep -v -E ':[0-9]+:\s*(//|\*)'
```

### 3.3 分批插，不要一次插满

| 优先级 | 插哪里 | 原因 |
|---|---|---|
| **第一批（必插）** | 均衡化代码里的集合通信（一般是全局负载统计 + 数据迁移，2~5 处） | 这是本次要检查的对象 |
| 第二批（按需） | 每个迭代末尾的全局同步（`Barrier`/`Allreduce`） | 如果报告显示等待热点在 `barrier` 而不是 `allreduce`/`alltoallv`，说明瓶颈在每轮同步上，这时才补 |
| 可选 | 其它与均衡化无关的集合通信 | 想区分"均衡化引起的等待"和"固有等待"时再插 |

**分批插的好处**：第一批插完就能出报告；如果等待累计都落在均衡化的 `allreduce`/`alltoallv` 上，第二批可以不做。

### 3.4 没有 git、只知道大概在哪个函数时怎么办

**第 1 步 · 定位函数**（按名字关键词搜）

```sh
grep -rniE 'balance|rebalance|repartition|redistribute|migrat|adapt|decomp|imbalance' \
  --include=*.c --include=*.h --include=*.cpp --include=*.hpp --include=*.cu . \
  | grep -vE ':[0-9]+:[[:space:]]*(//|\*)' | head -40
```

**第 2 步 · 列出该文件所有集合通信的行号**

```sh
grep -nE 'MPI_[A-Za-z_]+[[:space:]]*\(' 文件.c
```

结合第 1 步得到的函数定义行号，就能圈出哪些调用落在均衡化函数体内。

**第 3 步 · 顺着一层调用链找（别省这步）**

集合通信常被封装在辅助函数里（`compute_global_load()`、`migrate_partition()` 之类）。先看均衡化函数体里调用了哪些函数：

```sh
sed -n '<起行>,<止行>p' 文件.c | grep -oE '[A-Za-z_][A-Za-z0-9_]*[[:space:]]*\(' | sort -u
```

再对这些函数名重复第 2 步。**均衡化里真正的 `MPI_Alltoallv` 多半不在主函数里，而在被它调用的辅助函数里。**

**第 4 步 · 还是拿不准就"整片插"，让工具自己定位**

这是最实用的兜底策略，也是"只知道大概位置"时最省心的做法：把均衡化所在**整个文件或整个模块**里的集合通信全部用 `LB_WRAP` 包上（见 §4.5）。

- 每处只加**一行**，不用手编 tag；
- tag 自动等于源码行号，报告里看到 `tag=1234` 就知道是那个文件第 1234 行；
- 报告 §1「等待热点」按 tag 给出等待累计，**热点会自己浮现**——你不需要事先知道是哪一处。

代价只是每处一行，运行开销可忽略。等报告出来了，再决定要不要把没热点的插桩删掉。

### 3.5 先确认：均衡化代码到底跑在不在 MPI 进程里

这一步很容易被跳过，但跳过的代价很大。**"进程互相等待"只可能发生在多个进程之间**——如果均衡化是一段单进程运行的代码，它内部根本不可能产生互等，插桩也无从谈起。

典型的两种情况：

| 情况 | 特征 | 插桩点在哪 |
|---|---|---|
| **A. 均衡化在 MPI 程序内部** | 代码里能搜到 `MPI_Init`；均衡化函数直接调用 `MPI_Allreduce`/`MPI_Alltoallv` | 就在均衡化涉及的集合通信上（见 §3.2、§3.4） |
| **B. 均衡化是独立的前处理工具** | 单独的可执行文件，用 `./partition -balance pro_num=48 ...` 这样带参数直接跑，不经过 `mpirun` | 均衡化内部**没有**互等；真正的等待发生**主求解器**使用这个分区方案的地方，也就是主循环的集合通信上 |

判断方法（按信息量从高到低）：

```sh
cat testrun.sh          # 最直接: 实际用 mpirun 跑的是哪个可执行文件?
cat compile.sh          # 编译出几个可执行文件?
grep -rn 'MPI_Init' --include=*.cpp --include=*.c .   # 哪些程序是 MPI 并行的?
find . -name '*.cpp' -path '*balance*'                # 均衡化库的实现文件在哪?
```

如果落到情况 B，插桩点就不在均衡化代码里，而在**均衡化被调用之后的求解器主循环**上：均衡化改变了"哪个进程负责哪块网格"，这个改变带来的负载不均衡，会在主循环的集合通信点（每步的 `Barrier`/`Allreduce`/数据交换）上以等待的形式显现出来。这也正是本次要测的东西。

---

## 4. 插桩

### 4.0 插桩点到底在哪（最常见的误解）

**不是"均衡化函数的前后"，而是每一个集合通信（MPI 调用）的前后。**
一个均衡化函数里有几个集合通信，就插几对标记，每对用自己的 tag。

为什么不能插在函数前后——整套测量的基石是**"进入时刻"的含义**：

| 标记位置 | "进入时刻"实际含义 | 后果 |
|---|---|---|
| 集合通信前（正确） | 我算完了、到同步点、站在门口等人 | 算出的才是"等待" |
| 函数前（错误） | 我开始执行这个函数，可能还在算自己的局部负载 | 各 rank 的进入时刻差异里混进**本地计算时间**，埋头计算的进程被误记成"在等待"，等待时间虚高且归因全错 |

三种情形：

```c
/* ① 一个函数里只有一个集合通信 -> 一对 */
lb_wait_begin(LB_ALLREDUCE, 900);
MPI_Allreduce(&load, &max_load, 1, MPI_DOUBLE, MPI_MAX, MPI_COMM_WORLD);
lb_wait_end(LB_ALLREDUCE, 900, 1);

/* ② 一个函数里有多个 -> 各自一对, tag 各不相同 */
lb_wait_begin(LB_ALLREDUCE, 901);
MPI_Allreduce(&local_load, &sum_load, 1, MPI_DOUBLE, MPI_SUM, MPI_COMM_WORLD);
lb_wait_end(LB_ALLREDUCE, 901, 1);

/* 标记跟着调用走: 集合通信在 if 里, 标记就在 if 里 */
if (max_load > sum_load / nranks * 1.05) {
    lb_wait_begin(LB_ALLTOALLV, 902);
    MPI_Alltoallv(sbuf, scnt, sdisp, MPI_CHAR, rbuf, rcnt, rdisp, MPI_CHAR, MPI_COMM_WORLD);
    lb_wait_end(LB_ALLTOALLV, 902, 1);
}

/* ③ 非阻塞集合通信(CUDA/OpenACC 里常见): begin 在发起前, end 在 Wait 后 */
lb_wait_begin(LB_ALLREDUCE, 910);
MPI_Iallreduce(&load, &max_load, 1, MPI_DOUBLE, MPI_MAX, MPI_COMM_WORLD, &req);
...                                  /* 这段重叠区不要插桩 */
MPI_Wait(&req, MPI_STATUS_IGNORE);
lb_wait_end(LB_ALLREDUCE, 910, 1);
```

真正"卡住等人"的是 `Wait`，所以 `end` 必须在它之后。

**怎么确认插桩对上了**：跑一次看报告 §5 数据可靠性，"各 rank 调用次数不一致的同步点"应为 **0 处**，"未配对的 enter"应为"无"。

### 4.1 手工两行（推荐，最稳）

在集合通信前后各加一行，**别的都不动**：

```c
/* 改之前 */
MPI_Allreduce(&local_load, &max_load, 1, MPI_DOUBLE, MPI_MAX, MPI_COMM_WORLD);

/* 改之后 */
lb_wait_begin(LB_ALLREDUCE, 900);
MPI_Allreduce(&local_load, &max_load, 1, MPI_DOUBLE, MPI_MAX, MPI_COMM_WORLD);
lb_wait_end(LB_ALLREDUCE, 900, 1);
```

这种方式对**任何** MPI 调用都适用：`Bcast`、`Iallreduce` + `Wait`、`Sendrecv`、非阻塞的一整套都行，不受包装函数覆盖范围限制。

### 4.2 用包装函数替换（同样带插桩）

```c
double mx = lb_allreduce_max(900, local_load);      /* 内部已插桩 */
int    ok = lb_alltoallv(901, sendcnt, recvcnt);
lb_barrier(950);
```

可用的包装：`lb_barrier` / `lb_allreduce_max` / `lb_allreduce_sum` / `lb_allgather_int` / `lb_bcast_int` / `lb_alltoallv`。其它原语请用手工两行。

### 4.3 `kind` 和 `tag` 怎么填

**`kind`** 按原语类型选（决定报告里"等待热点"的分类）：

| 原语 | kind |
|---|---|
| `MPI_Barrier` | `LB_BARRIER` |
| `MPI_Allreduce` / `Reduce` | `LB_ALLREDUCE` |
| `MPI_Allgather` / `Allgatherv` | `LB_ALLGATHER` |
| `MPI_Alltoallv` / `Alltoall` | `LB_ALLTOALLV` |
| 点对点 `Send`/`Recv`/`Sendrecv` | `LB_SENDRECV` 或 `LB_RECV` |
| `MPI_Bcast` | `LB_BCAST` |

**`tag`** 规则很简单：**同一个调用点固定用同一个整数**。

- 一个调用点在循环里被执行很多次 → 仍然用同一个 tag，分析器按"第几次调用"自动配对；
- 建议给均衡化相关的调用点留一段区间（例如 900–949），每轮同步用另一段（例如 950–999）。这样报告里一眼能看出等待集中在哪一类；
- tag 只是标签，**不需要**和 `MPI_Send/Recv` 的 tag 有关联，也不会传到 MPI 层。

### 4.4 启动与收尾

```c
#include "lbprobe.h"          /* 或 "lbprobe_single.h" */

int main(int argc, char **argv) {
    MPI_Init(&argc, &argv);
    lb_init();                /* ① MPI_Init 之后调用一次 */
    ...
    lb_finalize();            /* ② MPI_Finalize 之前调用一次 */
    MPI_Finalize();
}
```

`lb_init()` 自己从 `MPI_COMM_WORLD` 取 rank/nranks，目录从环境变量 `LB_RUN_DIR` 取。

### 4.5 一行插桩宏 `LB_WRAP`（不确定位置时最省事）

```c
LB_WRAP(LB_ALLREDUCE,
        MPI_Allreduce(&load, &mx, 1, MPI_DOUBLE, MPI_MAX, MPI_COMM_WORLD));
```

它自动用**该行行号**当 tag，所以：

- 每个调用点只加一行，不用手编 tag，也就不会填错；
- 报告里 `tag=1234` 就是那个文件的**第 1234 行**，直接对得回去；
- 因此当你不确定热点在哪一处时，可以放心地**整片包**（见 §3.4 第 4 步），让报告自己指出问题。

**限制与坑**：

| 项 | 说明 |
|---|---|
| 只能包**单个调用语句** | 不要包 `if`/`for` 等语句块 |
| **只能包裸 MPI 调用** | `LB_WRAP(LB_ALLREDUCE, MPI_Allreduce(...))` ✅ ；`LB_WRAP(LB_BARRIER, lb_barrier(100))` ❌ —— `lb_barrier` 自身已插过桩，再包一层会把同一次同步**记两遍**，报告里等待翻倍 |
| 不要嵌套 | 同一个调用上不要套两个 `LB_WRAP` |
| 非阻塞通信不适用 | 请改用手工两行（`begin` 在发起前、`end` 在 `Wait` 之后），因为真正卡住等人的是 `Wait` |

> 本机自检已验证：`LB_WRAP` 产生的 tag 等于宏所在行号，且只产生一对 `enter`/`exit`；包 `lb_*` 包装函数时确实会产生嵌套的双份记录（这就是上表那条警告的由来）。

---

## 5. 改构建

### Makefile

```make
LBPROBE_DIR := $(HOME)/lbprobe

CFLAGS += -I$(LBPROBE_DIR)/include -DLBPROBE_HAVE_MPI
OBJS   += $(LBPROBE_DIR)/src/lbprobe.o

$(LBPROBE_DIR)/src/lbprobe.o: $(LBPROBE_DIR)/src/lbprobe.c
	$(CC) $(CFLAGS) -c $< -o $@
```

单文件版更简单：只需在**一个** .c 文件顶部加两行，其它文件只包含头，Makefile 里只加 `-I`：

```c
#define LBPROBE_IMPLEMENTATION     /* 整个项目只能有一个编译单元这样写! */
#include "lbprobe_single.h"
```

### CMake

```cmake
set(LBPROBE_DIR $ENV{HOME}/lbprobe)
target_sources(your_app PRIVATE ${LBPROBE_DIR}/src/lbprobe.c)
target_include_directories(your_app PRIVATE ${LBPROBE_DIR}/include)
target_compile_definitions(your_app PRIVATE LBPROBE_HAVE_MPI)
```

> 如果探针文件是**平铺**放在某个目录里的（没有 `include/`、`src/` 子目录），把上面两行换成：
> ```cmake
> target_sources(your_app PRIVATE ${LBPROBE_DIR}/lbprobe.c)
> target_include_directories(your_app PRIVATE ${LBPROBE_DIR})
> ```

> `lbprobe.c` 只用 C 标准库（`stdio/stdlib/string/stdarg/time`），**不需要**额外链接数学库或线程库。

### 关掉探针

| 做法 | 效果 |
|---|---|
| 编译时加 `-DLBPROBE_OFF` | 所有接口变成空宏，**零开销**，可用于正式生产构建 |
| 运行时 `lb_enable(0)` | 临时停采；`lb_enable(1)` 恢复。适合只测中间某一段 |
| 删掉那两行 + `#include` | 完全回退，`git diff` 很干净 |

建议正式测量前先跑一次 `-DLBPROBE_OFF` 与开启状态的对照，标定探针自身开销（目标 < 1%）。

---

## 6. 运行与分析

### 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `LB_RUN_DIR` | ✅ | 事件日志目录。**必须放节点本地盘**：`/dev/shm/lbprobe-$SLURM_JOB_ID` 或 `$TMPDIR`。**不要**放 Lustre/GPFS/NFS 家目录——逐事件写盘会拖慢并行文件系统，反过来干扰测量 |
| `LB_COLLECT_DIR` | 建议 | 共享目录。探针在 `lb_finalize()` 时把日志汇集过去，供离线分析 |

### Slurm 示例

```sh
#!/bin/sh
#SBATCH --job-name=lbmeasure
#SBATCH --nodes=4
#SBATCH --ntasks-per-node=32
#SBATCH --time=00:30:00

module load openmpi/4.x        # 换成你的

export LB_RUN_DIR=/dev/shm/lbprobe-$SLURM_JOB_ID
export LB_COLLECT_DIR=$SLURM_SUBMIT_DIR/lbmeasure_$SLURM_JOB_ID
mkdir -p $LB_RUN_DIR $LB_COLLECT_DIR

# timeout 兜底: 即使预期不会卡住也留着, 否则一旦异常会白占机时到墙钟上限
timeout -k 5 1500 srun ./your_app <你的输入参数>

python3 $HOME/lbprobe/tools/lb_analyze.py $LB_COLLECT_DIR
echo "报告: $LB_COLLECT_DIR/report.md"
```

### 生成报告

```sh
python3 tools/lb_analyze.py <日志目录>
# 产出: <日志目录>/report.md 与 analysis.json
```

阈值可以按你的判断口径调整：

```sh
python3 tools/lb_analyze.py <日志目录> \
    --wait-threshold 5 \        # 平均同时在等待的 rank 占比低于它 = 无实际影响
    --mutual-threshold 50 \     # 到齐前等待占全部等待的比例达到它 = 存在进程间等待
    --single-rank-threshold 60  # 单一进程占该等待的比例超过它 = 判为单向等待
```

> 分析脚本可以在**本地**跑：把 `LB_COLLECT_DIR` 整个拷回来，或让它指向两边都能访问的位置。

### 多个算例

每个算例跑一次、给一个独立的 `LB_COLLECT_DIR`，分别生成报告。报告里"进程数"字段会记录规模，便于横向比较。

---

## 7. 排错表

| 现象 | 原因与处理 |
|---|---|
| **`gcc: error: xxx.c: No such file or directory`** | 编译命令里的相对路径和你的实际布局不符。文档里的 `-Iinclude examples/... src/...` 是按本仓库的子目录结构写的；手工平铺复制时应改用 `-I. lb_minimal_mpi.c lbprobe.c`。先 `ls -la` 看文件到底在哪（见 §2） |
| **`mpirun ... could not access or execute ./lb_minimal`** | 几乎总是上一步编译失败导致可执行文件根本没生成，**不是 MPI 的问题**。往上翻编译输出先解决编译错误；或 `ls -l lb_minimal` 确认文件存在 |
| 编译报 `undefined reference to lb_init / lb_wait_begin ...` | 用单文件版但没定义实现宏。加 `-DLBPROBE_IMPLEMENTATION`，或把 `lbprobe.c` 加进命令行（二选一，不要都加） |
| 编译报 `implicit declaration of function 'clock_gettime'` | 严格 `-std=c99`/`-std=c11` 下 POSIX 时钟被隐藏。把 `#include "lbprobe.h"` 放到该文件第一行，或加 `-D_POSIX_C_SOURCE=200809L`（见 §2 情况 C） |
| `report.md` 报"运行目录里没有 events.rank*.jsonl" | `LB_RUN_DIR` 不对，或 `lb_init()` 没被调用。先 `ls $LB_RUN_DIR` 确认文件在不在 |
| 日志文件是空的 | 只调了 `lb_init()` 但没有任何 `lb_wait_begin/end`；或运行时被 `lb_enable(0)` 关掉了 |
| 报告里"缺失的 rank 日志"不为空 | `LB_COLLECT_DIR` 不是所有节点都可见，或部分 rank 异常退出。报告会显式告警 |
| 报告顶部出现"⚠ 数据不完整" | 同上。先补齐日志，不要采信结论 |
| 等待时间明显偏小 | 很可能只插了均衡化、没插每迭代的全局同步点（见 §3 第二批） |
| 想区分"均衡化引起"与"固有"等待 | 给两类调用点用不同 tag 段，报告里按 tag 分组看 |
| 编译报重复定义符号 | 单文件版：`LBPROBE_IMPLEMENTATION` 只能出现在一个编译单元；两文件版：确认没有同时用单文件版 |
| 编译报找不到 `lbprobe.h` | 漏了 `-I<头文件所在目录>`（子目录布局是 `-I.../include`，平铺布局是 `-I.`） |
| `report.md` 显示为乱码 | 报告是 UTF-8，用支持 UTF-8 的查看器或 `less`；Windows 记事本可能是旧编码 |
| 担心探针影响性能 | 先跑 `-DLBPROBE_OFF` 对照标定。单次插桩 = 2 次时钟读取 + 1 行文本写 |

---

## 8. 交付与回退

**交付物**：每个算例一份 `report.md`（中文），内含结论、等待时间、等待热点、责任归属、逐同步点明细、各 rank 分布、数据可靠性。附 `analysis.json` 供程序化处理。

**回退**：`git diff` 只会有三类改动——加 `#include`、加 `lb_init()/lb_finalize()`、每个集合通信前后各一行。构建里加 `-I` 与 `-DLBPROBE_HAVE_MPI`。全部可在几分钟内撤销，或直接用 `-DLBPROBE_OFF` 让它变成零开销空宏。

---

## 附：这份说明里哪些已经在本机验证过

| 项目 | 状态 |
|---|---|
| 两文件版 / 单文件版的编译与运行 | ✅ gcc 零警告编译，单文件版重复包含不冲突 |
| MPI 分支（`-DLBPROBE_HAVE_MPI`）的签名与类型 | ✅ 用严格签名的桩 mpi.h 做 `-fsyntax-only` 全部通过 |
| 分析器、报告生成、判定与四个算例 | ✅ 本机仿真链路跑通，管线自检 10/10 |
| `examples/lb_minimal_mpi.c` | ⚠ 本机无 MPI，只做了语法/类型检查；真编译请在集群上按 §2 执行 |
| 集群上的实际运行（`mpirun`/`srun`、共享文件系统） | ❌ 本机没有 MPI 环境，需要在集群上执行 §2 才能确认 |
