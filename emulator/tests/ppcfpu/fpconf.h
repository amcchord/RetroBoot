/*
 * fpconf: bare-metal PowerPC floating-point conformance and benchmark program
 * for qemu-system-ppc -M mac99 -cpu 7400 -bios fpconf.elf (no OS, real mode,
 * exception vectors in ROM at 0xfff00000).  Shared memory map (physical).
 */
#ifndef FPCONF_H
#define FPCONF_H

/* Parameter block: written by the driver with -device loader,data-be=on */
#define PARAM_BASE      0x00004000
#define P_MAGIC         0x00    /* 'FPCP' */
#define P_MODE          0x04    /* 1 = hash, 2 = log, 3 = bench, 5 = smc */
#define P_SEED_HI       0x08
#define P_SEED_LO       0x0c
#define P_COUNT         0x10    /* vectors per (cfg, op) */
#define P_CFGMASK       0x14    /* bit i: run configuration i */
#define P_OPFIRST       0x18    /* first op index */
#define P_OPLAST        0x1c    /* last op index (inclusive) */
#define P_BENCH_ITERS   0x20    /* bench: iterations per kernel */
#define P_BENCH_MASK    0x24    /* bench: kernels to run */
#define PARAM_MAGIC     0x46504350

/* Trap record (exception handlers) */
#define TRAP_BASE       0x00005000
#define TRAP_COUNT      0x5000
#define TRAP_SRR0       0x5004
#define TRAP_SRR1       0x5008
#define TRAP_RESUME     0x500c
#define TRAP_VECTOR     0x5010
#define TRAP_TOTAL      0x5014

/* Result header */
#define RESULT_BASE     0x00006000
#define R_DONE          0x00    /* 'DONE' when finished, 'FAIL' on error */
#define R_STATUS        0x04
#define R_NOPS          0x08
#define R_NCFGS         0x0c
#define R_NRECS         0x10    /* log mode: records written */
#define R_TBFREQ        0x14
#define R_VECTORS_LO    0x18    /* total vectors executed */
#define R_TRAPS         0x1c    /* total traps taken */
#define R_HASHES        0x100   /* [cfg][op] 64-bit hashes, cfg-major */
#define DONE_MAGIC      0x444f4e45
#define FAIL_MAGIC      0x4641494c

/* Bench results: [kernel] { u32 iters, u32 ops_per_iter, u64 ticks } */
#define BENCH_BASE      0x00020000

/* Log buffer: records of LOG_REC bytes */
#define LOG_BASE        0x01000000
#define LOG_REC         128     /* 16 header + 64 inputs + 48 outcome */

#define MAX_OPS         512
#define MAX_CFGS        16

#endif
