#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_single.py - 由 lbprobe.h + lbprobe.c 生成单文件版 lbprobe_single.h

为什么要生成而不是手写: 避免单文件版与两文件版走样(改了一处忘了另一处)。
用 Python 而不是 shell/PowerShell 是因为要按 UTF-8 精确读写中文注释:
Windows PowerShell 5.1 会按系统 ANSI(GBK)解析无 BOM 的脚本, 中文会乱码并破坏语法。

用法: python3 tools/make_single.py
生成后建议跑一次 tests/single_check.c 做编译自检。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

BANNER = """/* lbprobe_single.h - 单文件版 (头文件 + 实现合并, 由 tools/make_single.py 自动生成)
 *
 * 用法: 只在一个 .c 文件里定义实现宏, 其它文件只包含头。
 *     #define LBPROBE_IMPLEMENTATION     // 整个项目只能有一个编译单元这样写!
 *     #include "lbprobe_single.h"
 * 其它文件:
 *     #include "lbprobe_single.h"
 *
 * 编译注意: 探针需要一个亚毫秒时钟(clock_gettime), 它属于 POSIX 而非 C99 标准。
 *   若你的编译单元用严格的 -std=c99 / -std=c11(会定义 __STRICT_ANSI__),
 *   请二选一:
 *     ① 把 #include "lbprobe_single.h" 放在该文件的**第一行**(在所有系统头之前); 或
 *     ② 编译时加 -D_POSIX_C_SOURCE=200809L (或 -D_GNU_SOURCE)。
 *   用 C++ 编译或默认 gnu 模式则无需处理。
 *   (两文件版不受影响: lbprobe.c 自身第一行就定义了特性宏。)
 *
 * 不要手工编辑本文件; 改 lbprobe.h / lbprobe.c 后重新生成。
 */

/* 必须出现在任何系统头文件之前, 否则特性宏失效 */
#ifndef _POSIX_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#endif
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

"""

# 两层守卫:
#   外层 include guard 防重复声明; 内层实现守卫防同一编译单元内重复定义实现
MID = """
/* ==================== 实现部分 (需要 LBPROBE_IMPLEMENTATION) ==================== */
#ifdef LBPROBE_IMPLEMENTATION
#ifndef LBPROBE_IMPLEMENTATION_DONE
#define LBPROBE_IMPLEMENTATION_DONE

"""

TAIL = """
#endif /* LBPROBE_IMPLEMENTATION_DONE */
#endif /* LBPROBE_IMPLEMENTATION */
"""


def main():
    with open(os.path.join(ROOT, "include", "lbprobe.h"), encoding="utf-8") as f:
        hdr = f.read()
    with open(os.path.join(ROOT, "src", "lbprobe.c"), encoding="utf-8") as f:
        imp = f.read()

    # 实现文件里对自身头文件的包含要去掉(单文件版里已在同一文件内)
    imp = re.sub(r'#include "lbprobe\.h"\r?\n', "", imp, count=1)
    # 特性宏已提到文件最顶部, 去掉实现段里那份(避免重复定义)
    imp = re.sub(r"#define _GNU_SOURCE\r?\n", "", imp, count=1)

    out = BANNER + hdr + MID + imp + TAIL
    dst = os.path.join(ROOT, "lbprobe_single.h")
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    print("generated: %s (%d lines)" % (dst, out.count("\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
