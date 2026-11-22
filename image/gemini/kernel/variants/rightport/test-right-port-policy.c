// SPDX-License-Identifier: GPL-2.0
/*
 * Unit test of the Gemini right-port VBUS policy (variants/rightport).
 * check-src.sh extracts the code between the rightport-policy markers from
 * the patched drivers/usb/typec/gemini-right-port.c into policy.inc, so this
 * tests the exact kernel code: the scenarios of the on-device plan, then an
 * exhaustive check of every register value against the safety rules.
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef uint8_t u8;

#include "policy.inc"

static int fails, checks;

#define CHECK(cond, ...) do { checks++; if (!(cond)) { fails++; \
	printf("FAIL %s:%d: ", __FILE__, __LINE__); printf(__VA_ARGS__); printf("\n"); } } while (0)

#define SRC	GRP_MODE_SOURCE
#define ATT	GRP_STATUS_ATTACH
#define VOK	GRP_STATUS_VBUSOK
#define CC1	0x10
#define CC2	0x20
#define SINK	GRP_TYPE_SINK
#define SOURCE	GRP_TYPE_SOURCE

static struct grp_regs R(u8 mode, u8 status, u8 type)
{
	struct grp_regs r = { .ok = true, .mode = mode, .status = status, .type = type };
	return r;
}

static const struct grp_regs ERR = { .ok = false };

static bool step(struct grp_policy *p, struct grp_regs r)
{
	return grp_step(p, &r, true);
}

/* A policy that has been sourcing a USB device on CC1. */
static struct grp_policy sourcing(void)
{
	struct grp_policy p = { .state = GRP_IDLE };

	step(&p, R(SRC, ATT | CC1, SINK));
	step(&p, R(SRC, ATT | CC1, SINK));
	return p;
}

static void scenarios(void)
{
	struct grp_policy p = { .state = GRP_IDLE };
	int i;

	/* nothing attached, many polls: off */
	for (i = 0; i < 10; i++)
		CHECK(!step(&p, R(SRC, 0, 0)) && p.state == GRP_IDLE, "idle poll %d", i);

	/* USB-C charger on the right port: in SOURCE it does not attach (Rp/Rp) */
	for (i = 0; i < 10; i++)
		CHECK(!step(&p, R(SRC, 0, 0)), "charger, no attach");

	/* a charger that does attach (status as seen on the device: 0x29/0x08) */
	for (i = 0; i < 10; i++)
		CHECK(!step(&p, R(SRC, 0x29, 0x08)) && p.state == GRP_FOREIGN_VBUS,
		      "attached charger poll %d state %d", i, p.state);
	/* same without VBUS yet: other partner, still off */
	for (i = 0; i < 10; i++)
		CHECK(!step(&p, R(SRC, ATT | CC2, SOURCE)) && p.state == GRP_OTHER_PARTNER,
		      "source partner poll %d", i);

	/* A-to-C cable from a powered USB-A port: VBUS, no attach */
	for (i = 0; i < 10; i++)
		CHECK(!step(&p, R(SRC, VOK, 0)) && p.state == GRP_FOREIGN_VBUS, "legacy VBUS");

	/* USB stick, CC1: pending on the first poll, on at the second */
	p = (struct grp_policy){ .state = GRP_IDLE };
	CHECK(!step(&p, R(SRC, ATT | CC1, SINK)) && p.state == GRP_PENDING, "first poll pending");
	CHECK(step(&p, R(SRC, ATT | CC1, SINK)) && p.state == GRP_SOURCING, "second poll on");
	/* our own VBUS shows up as VBUSOK: stays on */
	for (i = 0; i < 10; i++)
		CHECK(step(&p, R(SRC, ATT | VOK | CC1, SINK)), "sourcing with own VBUS");
	/* unplugged: off on the very next poll, even with VBUS still decaying */
	CHECK(!step(&p, R(SRC, VOK, 0)) && !p.vbus, "unplug -> off");
	CHECK(!step(&p, R(SRC, 0, 0)) && p.state == GRP_IDLE, "then idle");

	/* USB stick on CC2, through an active cable (Ra) */
	p = (struct grp_policy){ .state = GRP_IDLE };
	step(&p, R(SRC, ATT | CC2, SINK | GRP_TYPE_ACTIVECABLE));
	CHECK(step(&p, R(SRC, ATT | CC2, SINK | GRP_TYPE_ACTIVECABLE)), "CC2 active cable on");

	/* the two confirming polls must match */
	p = (struct grp_policy){ .state = GRP_IDLE };
	step(&p, R(SRC, ATT | CC1, SINK));
	CHECK(!step(&p, R(SRC, ATT | CC2, SINK)), "flip between polls: not yet");
	CHECK(step(&p, R(SRC, ATT | CC2, SINK)), "then on");

	/* a gap (detach) between the two resets the count */
	p = (struct grp_policy){ .state = GRP_IDLE };
	step(&p, R(SRC, ATT | CC1, SINK));
	step(&p, R(SRC, 0, 0));
	CHECK(!step(&p, R(SRC, ATT | CC1, SINK)), "count reset by detach");

	/* VBUS present while a sink is first seen: never turned on */
	p = (struct grp_policy){ .state = GRP_IDLE };
	for (i = 0; i < 10; i++)
		CHECK(!step(&p, R(SRC, ATT | VOK | CC1, SINK)) && p.state == GRP_FOREIGN_VBUS,
		      "sink with foreign VBUS");
	/* ... then VBUS goes away: needs two clean polls */
	CHECK(!step(&p, R(SRC, ATT | CC1, SINK)), "clean poll 1");
	CHECK(step(&p, R(SRC, ATT | CC1, SINK)), "clean poll 2");

	/* while sourcing: partner turns into a source -> off at once */
	p = sourcing();
	CHECK(!step(&p, R(SRC, ATT | VOK | CC1, SOURCE)) && !p.vbus, "partner source -> off");
	/* while sourcing: I2C error -> off */
	p = sourcing();
	CHECK(!grp_step(&p, &ERR, true) && p.state == GRP_I2C_ERROR, "i2c error -> off");
	CHECK(!step(&p, R(SRC, ATT | CC1, SINK)), "after error: re-confirm (1)");
	CHECK(step(&p, R(SRC, ATT | CC1, SINK)), "after error: re-confirm (2)");
	/* while sourcing: Modes changed to DRP (old rb-gemini-typec) -> off */
	p = sourcing();
	CHECK(!step(&p, R(0x10, ATT | VOK | CC1, SINK)) && p.state == GRP_MODE_CHANGED,
	      "mode DRP -> off");
	/* while sourcing: allow_source=0 -> off, and stays off */
	p = sourcing();
	{
		struct grp_regs r = R(SRC, ATT | VOK | CC1, SINK);

		CHECK(!grp_step(&p, &r, false) && p.state == GRP_DISABLED, "disabled -> off");
		r = R(SRC, ATT | CC1, SINK);
		for (i = 0; i < 5; i++)
			CHECK(!grp_step(&p, &r, false), "disabled stays off");
	}
	/* orientation fault / none: off */
	p = (struct grp_policy){ .state = GRP_IDLE };
	for (i = 0; i < 5; i++)
		CHECK(!step(&p, R(SRC, ATT | 0x30, SINK)) && !step(&p, R(SRC, ATT, SINK)),
		      "bad orientation");
	/* audio / debug accessories: off */
	for (i = 0; i < 5; i++) {
		CHECK(!step(&p, R(SRC, ATT | CC1, 0x01)), "audio accessory");
		CHECK(!step(&p, R(SRC, ATT | CC1, SINK | 0x20)), "debug sink");
		CHECK(!step(&p, R(SRC, ATT | CC1, SINK | 0x01)), "sink + audio");
	}
}

/*
 * Every (mode, status, type) for every reachable policy state, checked
 * against the rules the driver promises.
 */
static void exhaustive(void)
{
	static const u8 modes[] = { 0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x11, 0xff };
	struct grp_policy starts[4];
	int s, m, st, ty, a;

	memset(starts, 0, sizeof(starts));
	starts[0].state = GRP_IDLE;				/* fresh */
	step(&starts[1], R(SRC, ATT | CC1, SINK));		/* pending */
	starts[2] = sourcing();					/* sourcing */
	step(&starts[3], R(SRC, ATT | CC2, SINK));		/* pending, CC2 */

	for (s = 0; s < 4; s++)
	for (m = 0; m < (int)sizeof(modes); m++)
	for (st = 0; st < 256; st++)
	for (ty = 0; ty < 256; ty++)
	for (a = 0; a < 2; a++) {
		struct grp_policy p = starts[s];
		struct grp_regs r = R(modes[m], st, ty);
		bool was = p.vbus, on = grp_step(&p, &r, a);
		unsigned int orient = (st >> 4) & 3;
		bool qualifies = r.mode == SRC && a && (st & ATT) && (orient == 1 || orient == 2) &&
				 (ty == SINK || ty == (SINK | GRP_TYPE_ACTIVECABLE));

		if (on != p.vbus)
			CHECK(0, "return %d != p.vbus %d", on, p.vbus);
		if (on && !qualifies)
			CHECK(0, "on for s%d mode 0x%02x status 0x%02x type 0x%02x allow %d",
			      s, r.mode, st, ty, a);
		if (on && (ty & SOURCE))
			CHECK(0, "on with a source partner type 0x%02x", ty);
		if (on && !was && (st & VOK))
			CHECK(0, "turned on with VBUS present s%d status 0x%02x", s, st);
		if (on && !was && s != 1 && s != 3)
			CHECK(0, "turned on in one poll from s%d", s);
		if (on && !was && s == 1 && (st != (ATT | CC1) || ty != SINK))
			CHECK(0, "confirmed by a different poll status 0x%02x type 0x%02x", st, ty);
		if (on && !was && s == 3 && (st != (ATT | CC2) || ty != SINK))
			CHECK(0, "confirmed (CC2) by a different poll status 0x%02x type 0x%02x", st, ty);
		if (was && qualifies && !on)
			CHECK(0, "dropped a qualifying USB device status 0x%02x type 0x%02x", st, ty);
		if ((unsigned int)p.state >= GRP_NR_STATES)
			CHECK(0, "bad state %d", p.state);
		checks++;
		if (fails > 20)
			return;
	}
	/* an I2C error from every state: off */
	for (s = 0; s < 4; s++) {
		struct grp_policy p = starts[s];

		CHECK(!grp_step(&p, &ERR, true) && !p.vbus && p.seen == 0, "error from s%d", s);
	}
}

/* Random sequences: VBUS only ever comes on after two identical clean polls. */
static void sequences(void)
{
	static const u8 statuses[] = { 0, ATT | CC1, ATT | CC2, ATT | VOK | CC1, VOK, ATT | 0x30, ATT };
	static const u8 types[] = { 0, SINK, SOURCE, SINK | GRP_TYPE_ACTIVECABLE, 0x01, SINK | SOURCE };
	unsigned int seed = 12345;
	int run, i;

	for (run = 0; run < 20000; run++) {
		struct grp_policy p = { .state = GRP_IDLE };
		struct grp_regs prev = { .ok = false };
		bool prev_clean = false;

		for (i = 0; i < 40; i++) {
			struct grp_regs r;
			bool was = p.vbus, on, clean;

			seed = seed * 1103515245 + 12345;
			if ((seed >> 16) % 23 == 0)
				r = ERR;
			else
				r = R((seed >> 8) % 17 ? SRC : 0x10, statuses[(seed >> 12) % 7],
				      types[(seed >> 20) % 6]);
			on = grp_step(&p, &r, (seed >> 4) % 31 != 0);
			clean = r.ok && r.mode == SRC && !(r.status & VOK) &&
				(r.status == (ATT | CC1) || r.status == (ATT | CC2)) &&
				(r.type == SINK || r.type == (SINK | GRP_TYPE_ACTIVECABLE));
			if (on && !was)
				CHECK(clean && prev_clean && prev.status == r.status &&
				      prev.type == r.type,
				      "run %d poll %d: on without two identical clean polls", run, i);
			if (on && r.ok && (r.type & SOURCE))
				CHECK(0, "run %d poll %d: on with a source", run, i);
			prev = r;
			prev_clean = clean;
			checks++;
		}
		if (fails > 20)
			return;
	}
}

int main(void)
{
	scenarios();
	exhaustive();
	sequences();
	printf("right-port policy: %d checks, %d failures\n", checks, fails);
	return fails ? 1 : 0;
}
