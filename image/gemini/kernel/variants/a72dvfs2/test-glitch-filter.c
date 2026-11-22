// SPDX-License-Identifier: GPL-2.0
/*
 * Unit test of the MT6797 thermal glitch filter (variants/a72dvfs2).
 * check-src.sh extracts the code between the glitch-filter markers from the
 * patched drivers/thermal/mediatek/auxadc_thermal.c into filter.inc, so this
 * tests the exact kernel code. Temperatures in mC, time in ms.
 */
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

typedef int64_t s64;
#define THERMAL_TEMP_INVALID	-274000

#include "filter.inc"

#define DELTA	15000
#define CONFIRM	200

static int fails;

#define CHECK(cond, ...) do { if (!(cond)) { fails++; printf("FAIL %s:%d: ", __FILE__, __LINE__); \
	printf(__VA_ARGS__); printf("\n"); } } while (0)

/* feed readings at the given times; write the reported values to out[] */
static void run(const int *in, const s64 *t, int n, int *out, bool *held, int delta)
{
	struct mtk_temp_filter f = { .last = INT_MIN, .held_since_ms = 0 };
	int i;

	for (i = 0; i < n; i++)
		out[i] = mtk_temp_filter_apply(&f, in[i], t[i], delta, CONFIRM, &held[i]);
}

static void expect(const char *name, const int *in, const s64 *t, const int *want, int n, int delta)
{
	int out[64];
	bool held[64];
	int i;

	run(in, t, n, out, held, delta);
	for (i = 0; i < n; i++)
		CHECK(out[i] == want[i], "%s: reading %d (%d mC at %lld ms) reported %d, want %d",
		      name, i, in[i], (long long)t[i], out[i], want[i]);
	printf("  %s: ok\n", name);
}

int main(void)
{
	const s64 t1s[] = { 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000 };
	const s64 t250[] = { 1000, 1250, 1500, 1750, 2000, 2250, 2500, 2750 };

	puts("glitch filter unit test");
	{	/* the on-glass case: one 99.2 C sample between 69.9 and 78.1 C */
		int in[] = { 69900, 99200, 78100, 77000 };
		int want[] = { 69900, 69900, 78100, 77000 };
		expect("single spike rejected", in, t1s, want, 4, DELTA);
	}
	{	/* a spike straight past the 105 C critical trip */
		int in[] = { 75000, 120000, 76000 };
		int want[] = { 75000, 75000, 76000 };
		expect("spike above critical rejected", in, t1s, want, 3, DELTA);
	}
	{	/* a real step that stays: reported one poll later */
		int in[] = { 70000, 95000, 95000, 95000 };
		int want[] = { 70000, 70000, 95000, 95000 };
		expect("sustained step confirmed on the next poll", in, t1s, want, 4, DELTA);
	}
	{	/* runaway in passive polling (250 ms): never two holds in a row */
		int in[] = { 60000, 80000, 100000, 120000, 140000 };
		int want[] = { 60000, 60000, 100000, 100000, 140000 };
		expect("runaway reported with at most one poll of lag", in, t250, want, 5, DELTA);
	}
	{	/* back-to-back sysfs reads of a latched spike cannot confirm it */
		int in[] = { 70000, 99000, 99000, 99000, 70000 };
		s64 t[] = { 1000, 2000, 2010, 2150, 3000 };
		int want[] = { 70000, 70000, 70000, 70000, 70000 };
		expect("reads closer than confirm_ms keep holding", in, t, want, 5, DELTA);
	}
	{	/* hot from the first reading: accepted at once */
		int in[] = { 106000, 106000 };
		int want[] = { 106000, 106000 };
		expect("first reading accepted", in, t1s, want, 2, DELTA);
	}
	{	/* slow real rise, 3 C per second: never held */
		int in[8], want[8], i;

		for (i = 0; i < 8; i++)
			in[i] = want[i] = 60000 + i * 3000;
		expect("gradual rise passes", in, t1s, want, 8, DELTA);
	}
	{	/* drops pass at once, also big ones */
		int in[] = { 90000, 50000, 90000 };
		int want[] = { 90000, 50000, 50000 };
		expect("drop accepted, big rise after it held", in, t1s, want, 3, DELTA);
	}
	{	/* invalid readings pass and do not change the state */
		int in[] = { 70000, THERMAL_TEMP_INVALID, 99000, 70000 };
		int want[] = { 70000, THERMAL_TEMP_INVALID, 70000, 70000 };
		expect("invalid passes through", in, t1s, want, 4, DELTA);
	}
	{	/* delta 0 = off */
		int in[] = { 70000, 99000, 70000 };
		int want[] = { 70000, 99000, 70000 };
		expect("filter off", in, t1s, want, 3, 0);
	}

	/*
	 * Properties over random sequences at thermal-core poll spacing
	 * (250 ms or 1 s): (a) never two consecutive held readings; (b) the
	 * report is the reading or a lower, earlier accepted value; (c) any
	 * temperature present in two consecutive readings is reported by the
	 * second one: min(in[i-1], in[i]) <= out[i].
	 */
	{
		int n = 200, round, i;
		int in[200], out[200];
		bool held[200];
		s64 t[200];

		srand(1);
		for (round = 0; round < 20000; round++) {
			int temp = 30000 + rand() % 60000;

			for (i = 0; i < n; i++) {
				int r = rand() % 100;

				if (r < 5)
					temp += 10000 + rand() % 60000;	/* jumps, some huge */
				else if (r < 10)
					temp -= rand() % 40000;
				else
					temp += rand() % 4001 - 2000;
				in[i] = (rand() % 50 == 0) ? temp + 15000 + rand() % 40000 : temp;	/* glitches */
				t[i] = (i ? t[i - 1] : 0) + ((round & 1) ? 250 : 1000);
			}
			run(in, t, n, out, held, DELTA);
			for (i = 0; i < n; i++) {
				CHECK(!(i && held[i] && held[i - 1]), "round %d: two holds in a row at %d", round, i);
				CHECK(out[i] <= in[i], "round %d: reported %d above reading %d", round, out[i], in[i]);
				if (i) {
					int both = in[i - 1] < in[i] ? in[i - 1] : in[i];

					CHECK(out[i] >= both, "round %d at %d: %d and %d read, reported %d",
					      round, i, in[i - 1], in[i], out[i]);
				}
			}
			if (fails > 20)
				break;
		}
		printf("  properties over 20000 random sequences x 200 polls: %s\n", fails ? "FAIL" : "ok");
	}

	printf("glitch filter: %s\n", fails ? "FAIL" : "pass");
	return fails != 0;
}
