/* fake_mpi/mpi.h - 仅用于本机语法检查的 MPI 桩头文件
 *
 * 本机没有 mpicc, 用它来对 examples/lb_minimal_mpi.c 做 -fsyntax-only 检查:
 * 至少能保证 MPI 调用的参数个数/类型/返回值用法没有笔误。
 * **不要**用它做真实编译 —— 它没有实现, 只有声明。
 *
 * 真实编译请在集群上: mpicc -DLBPROBE_HAVE_MPI ...
 */
#ifndef FAKE_MPI_H
#define FAKE_MPI_H

typedef int MPI_Comm;
typedef int MPI_Datatype;
typedef int MPI_Op;
typedef int MPI_Request;
typedef int MPI_Status;

#define MPI_COMM_WORLD 0
#define MPI_DOUBLE     1
#define MPI_MAX        2
#define MPI_SUM        3
#define MPI_INT        4
#define MPI_CHAR       5
#define MPI_STATUS_IGNORE ((MPI_Status *)0)

#ifdef __cplusplus
extern "C" {
#endif

int MPI_Init(int *argc, char ***argv);
int MPI_Initialized(int *flag);
int MPI_Finalize(void);
int MPI_Comm_rank(MPI_Comm comm, int *rank);
int MPI_Comm_size(MPI_Comm comm, int *size);
int MPI_Abort(MPI_Comm comm, int errcode);
int MPI_Barrier(MPI_Comm comm);
int MPI_Allreduce(const void *sendbuf, void *recvbuf, int count,
                  MPI_Datatype datatype, MPI_Op op, MPI_Comm comm);
int MPI_Alltoallv(const void *sendbuf, const int *sendcounts, const int *sdispls,
                  MPI_Datatype sendtype, void *recvbuf, const int *recvcounts,
                  const int *rdispls, MPI_Datatype recvtype, MPI_Comm comm);
int MPI_Allgather(const void *sendbuf, int sendcount, MPI_Datatype sendtype,
                  void *recvbuf, int recvcount, MPI_Datatype recvtype, MPI_Comm comm);
int MPI_Bcast(void *buffer, int count, MPI_Datatype datatype, int root, MPI_Comm comm);
int MPI_Sendrecv(const void *sendbuf, int sendcount, MPI_Datatype sendtype, int dest,
                 int sendtag, void *recvbuf, int recvcount, MPI_Datatype recvtype,
                 int source, int recvtag, MPI_Comm comm, MPI_Status *status);

#ifdef __cplusplus
}
#endif
#endif
