// SPDX-License-Identifier: GPL-2.0
/*
 * rb-cpumhz - measure one CPU's clock on the Gemini PDA (RetroBoot a72dvfs tests).
 *
 * Times a chain of dependent 64-bit adds pinned to one CPU: Cortex-A53 and A72
 * retire one dependent ADD per cycle, so adds / seconds = Hz (the loop's own
 * counter and branch issue alongside the chain). Best of five windows.
 *
 *   rb-cpumhz CPU [MS]      print the MHz of CPU (window MS, default 200)
 *   rb-cpumhz --burn CPU    keep CPU busy until killed (load for the step test)
 *
 * Built static by tests/make-a72-test-bundle.sh (aarch64-linux-gnu-gcc -static).
 */
#define _GNU_SOURCE
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static double now(void)
{
	struct timespec t;

	clock_gettime(CLOCK_MONOTONIC_RAW, &t);
	return t.tv_sec + t.tv_nsec * 1e-9;
}

static uint64_t chain(uint64_t n)
{
	uint64_t x = 0;

	while (n--)
		__asm__ volatile(".rept 64\n\tadd %0, %0, #1\n\t.endr" : "+r"(x));
	return x;
}

static int pin(int cpu)
{
	cpu_set_t set;

	CPU_ZERO(&set);
	CPU_SET(cpu, &set);
	if (sched_setaffinity(0, sizeof(set), &set)) {
		perror("sched_setaffinity");
		return -1;
	}
	return 0;
}

int main(int argc, char **argv)
{
	double best = 0, ms = 200, dt, t0;
	uint64_t iters = 1000;
	int cpu, i;

	if (argc >= 3 && !strcmp(argv[1], "--burn")) {
		if (pin(atoi(argv[2])))
			return 1;
		for (;;)
			chain(1u << 20);
	}
	if (argc < 2) {
		fprintf(stderr, "usage: %s CPU [MS] | --burn CPU\n", argv[0]);
		return 2;
	}
	cpu = atoi(argv[1]);
	if (argc >= 3)
		ms = atof(argv[2]);
	if (pin(cpu))
		return 1;
	/* calibrate the iteration count to about one window */
	for (;;) {
		t0 = now();
		chain(iters);
		dt = now() - t0;
		if (dt > ms / 4000.0)
			break;
		iters *= 2;
	}
	iters = (uint64_t)(iters * (ms / 1000.0) / dt) + 1;
	for (i = 0; i < 5; i++) {
		double mhz;

		t0 = now();
		chain(iters);
		dt = now() - t0;
		mhz = iters * 64.0 / dt / 1e6;
		if (mhz > best)
			best = mhz;
	}
	printf("%.0f\n", best);
	return 0;
}
