/* rb-bootdiag: Startup Details collector (ARCHITECTURE 16). The pure parts
 * (kernel log records, problem ranking, stage progress, the firmware
 * framebuffer's description and pixel format, report names, FAT labels) are
 * here so tests/test_bootdiag.c can check them; bootdiag.c does the I/O. */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <time.h>

#include "diagview.h"

/* ---- kernel log (/dev/kmsg records) ---- */
struct bd_kmsg {
    int prio;          /* 0 emerg .. 7 debug */
    uint64_t seq;
    uint64_t ts_us;    /* since boot (the kernel's log clock) */
    char text[RB_DIAG_LINE];
};
/* "fac+prio,seq,ts,flags[,...];text\n KEY=VALUE..." -> 0, or -1 when malformed.
 * Non-printable characters (and the kernel's \xNN escapes) become '?'/kept. */
int bd_kmsg_parse(const char *rec, size_t n, struct bd_kmsg *out);
/* How much a kernel message matters for Startup Details (0: not a problem;
 * 100: the reason the startup stopped). Firmware files that could not be
 * loaded, driver probes that failed, display driver errors, call traces and
 * RetroBoot's own panic notes rank first; ACPI table noise last. */
int bd_problem_score(int prio, const char *text);
/* RetroBoot's stage notes in the kernel log: "retroboot: stage <id> [detail]"
 * and "retroboot: panic <reason>" (id "panic"). Returns true for either. */
bool bd_marker(const char *text, char *id, size_t idn, char *detail, size_t detn);

/* ---- stages and the shared progress (permille) ---- */
enum bd_stage {
    BD_KERNEL,
    BD_MEDIUM_SEARCH,
    BD_MEDIUM_FOUND,
    BD_SQUASHFS,       /* checklist only, not in the progress contract */
    BD_ROOT,
    BD_SWITCH_ROOT,
    BD_DISPLAY,
    BD_PRESENTER,
    BD_HOSTD,
    BD_READY,
    BD_NSTAGES
};
struct bd_stages {
    bool done[BD_NSTAGES];
    bool failed[BD_NSTAGES];
    bool skipped[BD_NSTAGES];
    uint64_t ms[BD_NSTAGES];     /* when it was reached (ms since boot) */
    char detail[BD_NSTAGES][96];
    int published;               /* permille last written (never decreases) */
};
const char *bd_stage_id(int s);      /* "kernel", "medium-search", ... */
int bd_stage_permille(int s);         /* -1: not in the progress contract */
int bd_stage_from_id(const char *id); /* -1 when unknown */
/* The progress the contract allows: the last stage of the reached prefix
 * (a later stage implies the earlier ones, except that only `ready` implies
 * the display driver), never below `published`. *stage gets its index. */
int bd_progress(const struct bd_stages *st, int *stage);
/* The progress line: "<permille> <stage-id> <text>\n" (text <= 80 bytes). */
int bd_progress_line(int permille, int stage, const char *text, char *out, size_t cap);

/* ---- firmware framebuffer (boot_params screen_info) ---- */
struct bd_fbinfo {
    int type;          /* 0x23 VLFB (GRUB/VESA), 0x70 EFI */
    int w, h, depth, pitch;
    uint64_t base;
    uint8_t rpos, rsize, gpos, gsize, bpos, bsize;
};
int bd_screen_info(const uint8_t *buf, size_t n, struct bd_fbinfo *fi);
/* One row of XRGB8888 pixels in the framebuffer's format (32, 24 or 16 bpp). */
void bd_fb_convert_row(const uint32_t *src, uint8_t *dst, int w, const struct bd_fbinfo *fi);
/* ... and back (what is on screen, as XRGB8888). */
void bd_fb_decode_row(const uint8_t *src, uint32_t *dst, int w, const struct bd_fbinfo *fi);

/* ---- saved reports ---- */
#define BD_REPORT_DIR "retroboot/startup-details"
#define BD_REPORT_KEEP 10
/* "<YYYYmmdd-HHMMSS>-<bootid8>.txt" (UTC) */
void bd_report_name(time_t t, const char *boot_id, char *out, size_t cap);
bool bd_report_name_ok(const char *name);
/* Which of the names (any order) to delete so that at most `keep` remain,
 * never `own`: the oldest by name. Writes indices into del; returns the count. */
int bd_prune(char names[][64], int n, int keep, const char *own, int *del);
/* FAT volume label from a boot sector (FAT12/16 at 0x2b, FAT32 at 0x47),
 * trailing spaces removed. 0 when it is a FAT boot sector. */
int bd_fat_label(const uint8_t *bs, size_t n, char *label, size_t cap);

/* ---- panels ---- */
/* An EDID's identity: "SDC 4171 \"ATNA33AA02-0\", 30x19 cm" (maker, product
 * code, the name and text descriptors, the size). 0 when it is an EDID. */
int bd_edid_ident(const uint8_t *ed, size_t n, char *out, size_t cap);
/* amdgpu.dcdebugmask in words ("PSR off, Panel Replay off"; "defaults"). */
void bd_dcdebugmask_text(unsigned long mask, char *out, size_t cap);

/* ---- device-tree boards (Raspberry Pi 4/5; ARCHITECTURE 16) ---- */
/* The firmware's framebuffer through fbdev (simplefb, /dev/fb0) instead of
 * boot_params: the Raspberry Pi kernel has simplefb, no simpledrm. */
#define BD_FB_FBDEV 0x100
struct bd_fbdev {
    int w, h, bpp, pitch;
    uint8_t roff, rlen, goff, glen, boff, blen;
    uint64_t base;
};
/* fbdev ids of a firmware framebuffer rb-bootdiag may draw on while no KMS driver owns
 * the screen: simplefb ("simple") and the Raspberry Pi's bcm2708_fb ("BCM2708 FB"). */
bool bd_firmware_fbdev(const char *id);
/* bd_fbinfo from fbdev's variable and fixed screen information; 0 when usable. */
int bd_fbdev_info(const struct bd_fbdev *v, struct bd_fbinfo *fi);
/* A new-style Raspberry Pi revision code: "Raspberry Pi 4 Model B rev 1.5, BCM2711,
 * 4 GB (revision c03115)". Returns the memory in MB, or -1 for an old-style code. */
int bd_pi_revision(uint32_t code, char *out, size_t cap);
/* The bootloader's boot-mode (/proc/device-tree/chosen/bootloader/boot-mode, the
 * BOOT_ORDER codes): "SD card", "USB mass storage", "NVMe", ...; NULL when unknown. */
const char *bd_pi_boot_mode(unsigned mode);
/* An Arm core by /proc/cpuinfo's implementer and part ("Cortex-A72"); NULL unknown. */
const char *bd_arm_core(unsigned implementer, unsigned part);
/* USB mass-storage interfaces (bInterfaceClass 08) under usb_root (normally
 * /sys/bus/usb/devices) that no driver is bound to: "1-1.2 0781:5583 SanDisk Ultra"
 * per line in out[]; returns how many (at most max). */
int bd_usb_storage_unbound(const char *usb_root, char out[][128], int max);

/* ---- small text helpers ---- */
/* "4.2 s" style times (ms since boot) */
void bd_fmt_secs(uint64_t ms, char *out, size_t cap);
/* PCI vendor names for people ("AMD", "Intel", ...), NULL when unknown */
const char *bd_pci_vendor(unsigned vendor);
