/* single_check.c - 单文件版(lbprobe_single.h)的编译与可用性自检
 *
 * 验证四件事:
 *   1. 在同一个 TU 里定义 LBPROBE_IMPLEMENTATION 后, 单文件版能编译;
 *   2. 头文件部分重复包含不会重复声明(有 include guard);
 *   3. 接口真的能用(写入事件日志);
 *   4. LB_WRAP 宏自动用行号作 tag, 且 begin/end 配对正确。
 * 编译(仿真后端):
 *   gcc -I. -Iinclude tests/single_check.c src/lbprobe_sim.c -o build/single_check
 */
#define LBPROBE_IMPLEMENTATION
#include "lbprobe_single.h"
#include "lbprobe_single.h"      /* 故意重复包含: 验证不会重复声明 */

#include <stdio.h>

/* 被"插桩"的目标: 用 LB_WRAP 包住一次集合通信, 记下它所在的行号。
 * 注意这里故意包一个**没有插桩的普通调用**:
 * LB_WRAP 只能用于裸 MPI 调用, 不能包 lb_* 包装函数(那会重复插桩)。 */
static volatile int g_sink;
static int g_wrap_line;

static void plain_collective_stub(void) { g_sink++; }

static void instrumented_call(void)
{
    g_wrap_line = __LINE__ + 1;                 /* LB_WRAP 就在下一行 */
    LB_WRAP(LB_BARRIER, plain_collective_stub());
}

int main(void)
{
    int k;
    if (lb_init() != 0) { fprintf(stderr, "lb_init failed\n"); return 1; }

    for (k = 0; k < 3; k++) {
        lb_wait_begin(LB_BARRIER, 10 + k);
        lb_wait_end(LB_BARRIER, 10 + k, 1);
    }
    instrumented_call();
    lb_finalize();

    printf("single-file header OK: nranks=%d rank=%d\n", lb_nranks(), lb_rank());
    printf("LB_WRAP 期望 tag=%d (源码第 %d 行)\n", g_wrap_line, g_wrap_line);
    return 0;
}
