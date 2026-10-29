# Makefile - 同步点等待测量探针
#
# 插进你的项目只需要:
#   include/lbprobe.h + src/lbprobe.c    (130 + 326 行, 无线程无依赖)
# 或者用单文件版(由 tools/make_single.py 生成, 不入库):
#   lbprobe_single.h                     (490 行, 头+实现合并)
# 编译时加 -DLBPROBE_HAVE_MPI, 并把 -Iinclude 加上即可。
#
# 本仓库自带的仿真算例与仿真后端只用于本机/CI 验证, 不要插进项目:
#   src/lbprobe_sim.c            (无 MPI 时的假集合通信)
#   tests/lb_scenarios.c         (验证用算例)
#
# 集群(真 MPI):  make MPI=1 && NP=64 ITERS=20 make run
# 本机(仿真):    make && make sim-all        (需要 POSIX sh, 即 Linux/macOS/WSL)

MPI      ?= 0
# 注意: 不能写 `CC ?= gcc` —— make 内建了 CC=cc, `?=` 不生效, 在没装 `cc`
# 别名的系统(如 Windows/MinGW)上会直接报 "cc: command not found"。
CC       = gcc
OPT      ?= -O2 -g
WARN     ?= -Wall -Wextra
INC      := -Iinclude
BUILD    := build
NP       ?= 8
ITERS    ?= 8

ifeq ($(MPI),1)
  CC      := mpicc
  CFLAGS  += -DLBPROBE_HAVE_MPI
else
  SIMSRC  := src/lbprobe_sim.c
endif

CFLAGS += $(OPT) $(WARN) $(INC) -std=c99 -D_POSIX_C_SOURCE=200809L
LDLIBS += -lm

BIN  := $(BUILD)/lb_scenarios
SRCS := src/lbprobe.c $(SIMSRC) tests/lb_scenarios.c

.PHONY: all clean run sim-all example

all: $(BIN)

$(BIN): $(SRCS) include/lbprobe.h
	@mkdir -p $(BUILD)
	$(CC) $(CFLAGS) $(SRCS) -o $@ $(LDLIBS)

# 集群: 用 timeout 兜底。即使预期不会卡住, 这层保护也要有,
# 否则一旦异常, 作业会占着机时直到墙钟上限被调度器杀掉。
run: $(BIN)
	NP=$(NP) ITERS=$(ITERS) sh tools/run_all.sh

# 本机: 无 MPI 的仿真跑套
sim-all:
	@sh tools/run_all_sim.sh

clean:
	rm -rf $(BUILD) runs
