/* rb-bootdiag: Startup Details (ARCHITECTURE 16, presenter/README.md).
 *
 * Started from the initramfs (init-top) next to the splash and again in the
 * real root by retroboot-bootdiag.service; /run/retroboot/bootdiag/ carries
 * its state across switch_root. It
 *   - collects the facts of this startup (kernel log, stages, hardware) and
 *     publishes the startup progress for the installer splash;
 *   - listens for Escape on every keyboard (evdev) until the presenter owns
 *     input, and shows/hides the view (the `visible` file);
 *   - draws the view itself on the firmware framebuffer while no KMS driver
 *     owns the display (the splash or the presenter draw it otherwise);
 *   - saves a plain-text report to the installer's USB stick (RBINSTEFI).
 * Libc only. */
#include "bootdiag.h"

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <fnmatch.h>
#include <limits.h>
#include <linux/fb.h>
#include <linux/input.h>
#include <poll.h>
#include <linux/netlink.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/inotify.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "diagview.h"
#include "log.h"
#include "loop.h"
#include "scale.h"

#define TICK_MS 500
#define RING 400
#define MAXPROB 48
#define MAXIN 24
#define MAXPCI 8
#define HINT_MS 45000
#define AUTOSAVE1_MS 90000
#define AUTOSAVE2_MS 180000
#define SAVE_EVERY_MS 15000
#define DISPLAY_GRACE_MS 20000 /* after switch_root: no display driver by then is a failure */
#define FB_SETTLE_MS 1500      /* a failed driver probe: draw again only after it settled */
#define SAVE_TIMEOUT_MS 60000
#define REPORT_CAP (512 * 1024)

#define FB_PLATFORM_DEVS "efi-framebuffer.0", "vesa-framebuffer.0", "simple-framebuffer.0"

struct problem {
    int score;
    uint64_t ms;
    int count;
    char text[RB_DIAG_LINE];
};

struct pcidev {
    char addr[16];
    unsigned vendor, device, svendor, sdevice, cls;
    char driver[32];
    bool boot_vga, has_fb;
    char expect[32];
    bool expect_done;
    char probe_err[96]; /* last failed probe from the kernel log */
};

struct indev {
    int fd;
    char path[40];
    char name[80];
    struct rb_watch *w;
};

struct prstate {
    bool present, alive;
    int pid;
    bool input, display, hostd, visible;
    char scene[16], view[16], driver[32], device[32], mode[32];
    uint64_t start_ms, input_ms, view_ms;
    long age_s; /* since the presenter last wrote or touched its file (heartbeat) */
};

/* Stalled display updates a drawer published (rb_diag_publish_flips). */
struct flipinfo {
    bool present;
    unsigned stalls, slow;
    uint64_t first_ms, last_ms, slow_max;
    char conn[32], drv[32];
};

struct bd {
    struct rb_loop *loop;
    struct rb_diag_cfg cfg;
    bool initramfs;
    uint64_t start_ms;
    char boot_id[40];
    char build[96];     /* RetroBoot build / version */
    /* kernel log */
    int kmsg_fd;
    uint64_t kmsg_seq;
    uint64_t seq_known;       /* records up to here were counted by the initramfs instance */
    struct bd_kmsg *ring;
    int ring_head, ring_n;
    struct problem prob[MAXPROB];
    int nprob;
    struct { char addr[16], text[96]; } probe_fail[MAXPCI];
    int nprobe_fail;
    /* stages */
    struct bd_stages st;
    char pub_line[160];
    /* hardware */
    char dmi[3][160];   /* computer, BIOS, board (device-tree boards: model, bootloader, revision) */
    char cpu[160];
    char mem[32];
    bool dt;            /* a device-tree board without DMI (Raspberry Pi) */
    bool pi;            /* ... a Raspberry Pi (its firmware's bootloader node or revision code) */
    char usbstor[4][128]; /* USB mass-storage devices no driver took */
    int nusbstor;
    struct pcidev pci[MAXPCI];
    int npci;
    char medium[64];
    /* input */
    struct indev in[MAXIN];
    int nl_fd, ino_fd;
    /* presenter */
    struct prstate pr;
    struct flipinfo flips[2]; /* presenter, splash */
    bool debugfs_tried;
    /* firmware framebuffer */
    struct bd_fbinfo fi;
    bool fi_ok;
    bool fbdev;                /* fi is /dev/fb0 (simplefb on a Raspberry Pi), not boot_params */
    int fb_bar_pci;            /* index into pci[] whose BAR holds the framebuffer, -1 unknown */
    uint8_t *fb;               /* mapping */
    size_t fb_len, fb_maplen, fb_off;
    uint8_t *under;            /* what was on screen before we drew (native format) */
    bool under_ok;
    uint8_t *shown;            /* what we believe is on screen */
    bool shown_ok;
    uint32_t *frame;           /* XRGB composition */
    bool fb_touched;           /* our pixels are (or may be) on screen */
    uint64_t fb_nodrv_since;   /* the boot display has had no driver since (after its platform device went) */
    bool fb_was_allowed;
    char fb_state[96];
    uint64_t fb_view_gen;      /* doc generation drawn */
    /* view */
    bool visible, hint;
    struct rb_diag_doc doc;
    char *doc_text;
    uint64_t doc_gen, view_written_ms;
    bool dirty;
    bool opened_ever;
    uint64_t hide_ms, visible_since;
    /* failed units (root instance) */
    char failed_units[512];
    uint64_t failed_ms;
    /* saves */
    char save_name[64];
    char saved_where[200];
    char save_err[200];
    pid_t save_pid;
    int save_fd;
    struct rb_watch *save_watch;
    uint64_t save_started, last_save_start, last_save_done_start;
    bool save_again;
    char save_reason[80], save_last_reason[80];
    uint64_t save_retry_at;    /* the stick was busy: try again then */
    bool save_busy;
    bool auto1, auto2;
    struct rb_timer *tick;
    bool quit_pending;
};

static struct bd B;

/* snprintf for text that may be cut (labels, paths, kernel messages): the
 * cut is intended, so no truncation warnings. */
static void sfmt(char *dst, size_t cap, const char *f, ...) __attribute__((format(printf, 3, 4)));
static void sfmt(char *dst, size_t cap, const char *f, ...)
{
    va_list ap;
    va_start(ap, f);
    vsnprintf(dst, cap, f, ap);
    va_end(ap);
}

/* ------------------------------------------------------------------------ */
/* small helpers                                                            */

static uint64_t now_ms(void) { return rb_boot_ms(); }

static void trim(char *s)
{
    size_t n = strlen(s);
    while (n && isspace((unsigned char)s[n - 1]))
        s[--n] = 0;
    size_t k = 0;
    while (s[k] && isspace((unsigned char)s[k]))
        k++;
    if (k)
        memmove(s, s + k, n - k + 1);
}

static bool read_trim(const char *path, char *buf, size_t cap)
{
    if (rb_diag_read_file(path, buf, cap) < 0)
        return false;
    trim(buf);
    return buf[0] != 0;
}

static bool link_base(const char *path, char *out, size_t cap)
{
    char t[PATH_MAX];
    ssize_t n = readlink(path, t, sizeof t - 1);
    if (n <= 0) {
        out[0] = 0;
        return false;
    }
    t[n] = 0;
    const char *b = strrchr(t, '/');
    sfmt(out, cap, "%s", b ? b + 1 : t);
    return true;
}

static bool exists(const char *path) { return access(path, F_OK) == 0; }


/* growable text */
struct buf {
    char *p;
    size_t n, cap;
};
static void bprintf(struct buf *b, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
static void bprintf(struct buf *b, const char *fmt, ...)
{
    if (!b->p || b->n + 1 >= b->cap)
        return;
    va_list ap;
    va_start(ap, fmt);
    int k = vsnprintf(b->p + b->n, b->cap - b->n, fmt, ap);
    va_end(ap);
    if (k > 0)
        b->n = b->n + (size_t)k < b->cap ? b->n + (size_t)k : b->cap - 1;
}

/* ------------------------------------------------------------------------ */
/* hardware facts                                                           */

/* A big-endian u32 device-tree property. */
static bool dt_u32(const char *path, uint32_t *v)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return false;
    uint8_t x[4];
    bool ok = read(fd, x, 4) == 4;
    close(fd);
    if (ok)
        *v = (uint32_t)x[0] << 24 | (uint32_t)x[1] << 16 | (uint32_t)x[2] << 8 | x[3];
    return ok;
}

#define DT_BOOTLOADER "/proc/device-tree/chosen/bootloader"

/* The Raspberry Pi bootloader: its build date, what it started from, partition and
 * the tryboot flag (Pi 4/5 EEPROM bootloader; absent elsewhere). */
static void pi_bootloader(char *out, size_t cap)
{
    out[0] = 0;
    uint32_t ts = 0, mode = 0, part = 0, tryb = 0;
    char date[32] = "(date unknown)";
    if (dt_u32(DT_BOOTLOADER "/build-timestamp", &ts) && ts) {
        time_t t = (time_t)ts;
        struct tm g;
        gmtime_r(&t, &g);
        strftime(date, sizeof date, "%Y-%m-%d", &g);
    }
    bool have_mode = dt_u32(DT_BOOTLOADER "/boot-mode", &mode);
    if (!ts && !have_mode)
        return;
    char from[96] = "", pn[24] = "";
    const char *m = have_mode ? bd_pi_boot_mode(mode) : NULL;
    if (m)
        sfmt(from, sizeof from, ", started from the %s", m);
    else if (have_mode)
        sfmt(from, sizeof from, ", boot mode 0x%x", mode);
    if (dt_u32(DT_BOOTLOADER "/partition", &part) && part)
        sfmt(pn, sizeof pn, " partition %u", part);
    bool tried = dt_u32(DT_BOOTLOADER "/tryboot", &tryb) && tryb;
    sfmt(out, cap, "bootloader %s%s%s%s", date, from, pn, tried ? " (tryboot)" : "");
}

/* A device-tree board without DMI: the model, the firmware's revision code (a
 * Raspberry Pi's board, SoC and memory), its bootloader, and the Arm cores. */
static void facts_dt(struct bd *b)
{
    char model[160] = "";
    if (rb_diag_read_file("/proc/device-tree/model", model, sizeof model) <= 0 || !model[0])
        return;
    trim(model);
    b->dt = true;
    sfmt(b->dmi[0], sizeof b->dmi[0], "%s", model);
    b->pi = !strncmp(model, "Raspberry Pi", 12) || exists(DT_BOOTLOADER);
    pi_bootloader(b->dmi[1], sizeof b->dmi[1]);
    uint32_t rev;
    b->dmi[2][0] = 0;
    if (dt_u32("/proc/device-tree/system/linux,revision", &rev))
        bd_pi_revision(rev, b->dmi[2], sizeof b->dmi[2]);
}

/* Arm: /proc/cpuinfo has no "model name"; the core from implementer and part, the SoC
 * from the device tree's compatible list ("brcm,bcm2711"). */
static void cpu_dt(char *out, size_t cap, int threads)
{
    FILE *f = fopen("/proc/cpuinfo", "re");
    unsigned impl = 0, part = 0;
    bool hi = false, hp = false;
    if (f) {
        char line[256];
        while (fgets(line, sizeof line, f) && !(hi && hp)) {
            if (!hi && sscanf(line, "CPU implementer : %x", &impl) == 1)
                hi = true;
            if (!hp && sscanf(line, "CPU part : %x", &part) == 1)
                hp = true;
        }
        fclose(f);
    }
    char soc[32] = "", compat[256];
    ssize_t n = rb_diag_read_file("/proc/device-tree/compatible", compat, sizeof compat);
    for (ssize_t i = 0; i < n; i += (ssize_t)strlen(compat + i) + 1)
        if (!strncmp(compat + i, "brcm,bcm", 8) && !soc[0]) {
            sfmt(soc, sizeof soc, "%s", compat + i + 5);
            for (char *q = soc; *q; q++)
                *q = (char)toupper((unsigned char)*q);
        }
    const char *core = hi && hp ? bd_arm_core(impl, part) : NULL;
    char c[48] = "";
    if (core)
        sfmt(c, sizeof c, "%s", core);
    else if (hp)
        sfmt(c, sizeof c, "Arm core 0x%x", part);
    sfmt(out, cap, "%s%s%s", soc, soc[0] && c[0] ? " " : "", c);
    if (threads) {
        size_t l = strlen(out);
        sfmt(out + l, cap - l, " (%d thread%s)", threads, threads == 1 ? "" : "s");
    }
}

static void facts_static(struct bd *b)
{
    char v[96] = "", p[96] = "", ver[96] = "", bv[64] = "", bver[64] = "", bdate[32] = "", bn[96] = "";
    read_trim("/sys/class/dmi/id/sys_vendor", v, sizeof v);
    read_trim("/sys/class/dmi/id/product_name", p, sizeof p);
    read_trim("/sys/class/dmi/id/product_version", ver, sizeof ver);
    read_trim("/sys/class/dmi/id/bios_vendor", bv, sizeof bv);
    read_trim("/sys/class/dmi/id/bios_version", bver, sizeof bver);
    read_trim("/sys/class/dmi/id/bios_date", bdate, sizeof bdate);
    read_trim("/sys/class/dmi/id/board_name", bn, sizeof bn);
    /* Lenovo puts the model in product_version and the machine type in product_name */
    bool useful_ver = ver[0] && strcasecmp(ver, "none") && strcasecmp(ver, "Not Specified") &&
                      strcasecmp(ver, "To be filled by O.E.M.") && strcmp(ver, p);
    if (useful_ver && strlen(ver) > 3 && !isdigit((unsigned char)ver[0]))
        sfmt(b->dmi[0], sizeof b->dmi[0], "%s %s (%s)", v, ver, p);
    else
        sfmt(b->dmi[0], sizeof b->dmi[0], "%s %s%s%s", v, p, useful_ver ? " " : "", useful_ver ? ver : "");
    trim(b->dmi[0]);
    if (!b->dmi[0][0])
        sfmt(b->dmi[0], sizeof b->dmi[0], "unknown computer");
    sfmt(b->dmi[1], sizeof b->dmi[1], "%s %s %s", bv, bver, bdate);
    trim(b->dmi[1]);
    sfmt(b->dmi[2], sizeof b->dmi[2], "%s", bn);
    if (!v[0] && !p[0])
        facts_dt(b);

    FILE *f = fopen("/proc/cpuinfo", "re");
    int threads = 0;
    b->cpu[0] = 0;
    if (f) {
        char line[512];
        while (fgets(line, sizeof line, f)) {
            if (!strncmp(line, "processor", 9))
                threads++;
            if (!b->cpu[0] && !strncmp(line, "model name", 10)) {
                char *c = strchr(line, ':');
                if (c) {
                    sfmt(b->cpu, sizeof b->cpu, "%s", c + 1);
                    trim(b->cpu);
                }
            }
        }
        fclose(f);
    }
    if (!b->cpu[0] && b->dt) {
        cpu_dt(b->cpu, sizeof b->cpu, threads);
    } else if (threads) {
        size_t n = strlen(b->cpu);
        sfmt(b->cpu + n, sizeof b->cpu - n, " (%d thread%s)", threads, threads == 1 ? "" : "s");
    }
    f = fopen("/proc/meminfo", "re");
    if (f) {
        char line[256];
        unsigned long long kb = 0;
        while (fgets(line, sizeof line, f))
            if (sscanf(line, "MemTotal: %llu kB", &kb) == 1)
                break;
        fclose(f);
        sfmt(b->mem, sizeof b->mem, "%.1f GB", kb / 1048576.0);
    }
    /* the build: the root's release file, else the installer medium's marker */
    char rel[512];
    if (rb_diag_read_file("/usr/lib/retroboot/release.json", rel, sizeof rel) > 0) {
        char version[32] = "", flavor[32] = "", build[64] = "";
        const char *k;
        if ((k = strstr(rel, "\"version\": \"")))
            sscanf(k + 12, "%31[^\"]", version);
        if ((k = strstr(rel, "\"flavor\": \"")))
            sscanf(k + 11, "%31[^\"]", flavor);
        if ((k = strstr(rel, "\"build_id\": \"")))
            sscanf(k + 13, "%63[^\"]", build);
        sfmt(b->build, sizeof b->build, "RetroBoot %s %s, build %s", version, flavor, build);
    } else {
        char cmd[4096];
        rb_diag_read_file("/proc/cmdline", cmd, sizeof cmd);
        const char *m = strstr(cmd, "rb.live=.retroboot-installer-");
        if (m) {
            char id[64] = "";
            sscanf(m + 29, "%63[^ \n]", id);
            sfmt(b->build, sizeof b->build, "RetroBoot, build %s", id);
        } else {
            sfmt(b->build, sizeof b->build, "RetroBoot");
        }
    }
}

/* A module named by modprobe.blacklist= on the command line. */
static bool blocked(const char *mod)
{
    if (!mod || !mod[0])
        return false;
    char cmd[4096];
    if (rb_diag_read_file("/proc/cmdline", cmd, sizeof cmd) <= 0)
        return false;
    for (char *p = strstr(cmd, "modprobe.blacklist="); p; p = strstr(p + 1, "modprobe.blacklist=")) {
        char list[256];
        if (sscanf(p + 19, "%255s", list) != 1)
            continue;
        char *save = NULL;
        for (char *m = strtok_r(list, ",", &save); m; m = strtok_r(NULL, ",", &save)) {
            char a[64], c[64];
            sfmt(a, sizeof a, "%s", m);
            sfmt(c, sizeof c, "%s", mod);
            for (char *q = a; *q; q++)
                if (*q == '-')
                    *q = '_';
            for (char *q = c; *q; q++)
                if (*q == '-')
                    *q = '_';
            if (!strcmp(a, c))
                return true;
        }
    }
    return false;
}

static bool pci_expect(struct pcidev *d)
{
    if (d->expect_done)
        return d->expect[0];
    d->expect_done = true;
    char path[PATH_MAX], alias[256];
    sfmt(path, sizeof path, "/sys/bus/pci/devices/%s/modalias", d->addr);
    if (!read_trim(path, alias, sizeof alias))
        return false;
    struct utsname u;
    if (uname(&u))
        return false;
    sfmt(path, sizeof path, "/lib/modules/%s/modules.alias", u.release);
    FILE *f = fopen(path, "re");
    if (!f)
        return false;
    char line[512];
    while (fgets(line, sizeof line, f)) {
        char pat[400], mod[64];
        if (strncmp(line, "alias pci:", 10) || sscanf(line, "alias %399s %63s", pat, mod) != 2)
            continue;
        if (!fnmatch(pat, alias, 0)) {
            sfmt(d->expect, sizeof d->expect, "%s", mod);
            break;
        }
    }
    fclose(f);
    return d->expect[0];
}

static void pci_scan(struct bd *b)
{
    struct pcidev old[MAXPCI];
    int nold = b->npci;
    memcpy(old, b->pci, sizeof old);
    b->npci = 0;
    b->fb_bar_pci = -1;
    DIR *dir = opendir("/sys/bus/pci/devices");
    if (!dir)
        return;
    struct dirent *e;
    while ((e = readdir(dir)) && b->npci < MAXPCI) {
        if (e->d_name[0] == '.')
            continue;
        char path[PATH_MAX], v[64];
        sfmt(path, sizeof path, "/sys/bus/pci/devices/%s/class", e->d_name);
        if (!read_trim(path, v, sizeof v))
            continue;
        unsigned cls = (unsigned)strtoul(v, NULL, 16);
        if ((cls >> 16) != 0x03)
            continue;
        struct pcidev *d = &b->pci[b->npci++];
        memset(d, 0, sizeof *d);
        sfmt(d->addr, sizeof d->addr, "%s", e->d_name);
        d->cls = cls;
        for (int i = 0; i < nold; i++)
            if (!strcmp(old[i].addr, d->addr)) {
                memcpy(d->expect, old[i].expect, sizeof d->expect);
                d->expect_done = old[i].expect_done;
            }
#define RDHEX(field, name)                                                                                             \
    sfmt(path, sizeof path, "/sys/bus/pci/devices/%s/" name, d->addr);                                             \
    if (read_trim(path, v, sizeof v))                                                                                  \
        d->field = (unsigned)strtoul(v, NULL, 16);
        RDHEX(vendor, "vendor")
        RDHEX(device, "device")
        RDHEX(svendor, "subsystem_vendor")
        RDHEX(sdevice, "subsystem_device")
#undef RDHEX
        sfmt(path, sizeof path, "/sys/bus/pci/devices/%s/boot_vga", d->addr);
        d->boot_vga = read_trim(path, v, sizeof v) && v[0] == '1';
        sfmt(path, sizeof path, "/sys/bus/pci/devices/%s/driver", d->addr);
        link_base(path, d->driver, sizeof d->driver);
        if (b->fi_ok) {
            sfmt(path, sizeof path, "/sys/bus/pci/devices/%s/resource", d->addr);
            FILE *f = fopen(path, "re");
            if (f) {
                unsigned long long s, en, fl;
                while (fscanf(f, "%llx %llx %llx", &s, &en, &fl) == 3)
                    if (s && b->fi.base >= s && b->fi.base <= en) {
                        d->has_fb = true;
                        b->fb_bar_pci = b->npci - 1;
                    }
                fclose(f);
            }
        }
        for (int i = 0; i < b->nprobe_fail; i++)
            if (!strcmp(b->probe_fail[i].addr, d->addr))
                sfmt(d->probe_err, sizeof d->probe_err, "%s", b->probe_fail[i].text);
    }
    closedir(dir);
}

/* DRM devices with a driver (cardN, not connectors). */
static int drm_cards(char names[][16], char drivers[][32], int max)
{
    DIR *dir = opendir("/sys/class/drm");
    if (!dir)
        return 0;
    int n = 0;
    struct dirent *e;
    while ((e = readdir(dir)) && n < max) {
        if (strncmp(e->d_name, "card", 4) || strchr(e->d_name, '-') || !isdigit((unsigned char)e->d_name[4]))
            continue;
        char path[PATH_MAX];
        sfmt(names[n], 16, "%.15s", e->d_name);
        sfmt(path, sizeof path, "/sys/class/drm/%s/device/driver", e->d_name);
        if (!link_base(path, drivers[n], 32))
            sfmt(drivers[n], 32, "?");
        n++;
    }
    closedir(dir);
    return n;
}

/* DRM devices that can show something: v3d (a Raspberry Pi's render-only GPU, no
 * connectors) is not one. Elsewhere the same as drm_cards. */
static int kms_cards(char names[][16], char drivers[][32], int max)
{
    char n2[8][16], d2[8][32];
    int n = drm_cards(n2, d2, 8), k = 0;
    for (int i = 0; i < n && k < max; i++) {
        if (!strcmp(d2[i], "v3d"))
            continue;
        memcpy(names[k], n2[i], sizeof n2[i]);
        memcpy(drivers[k], d2[i], sizeof d2[i]);
        k++;
    }
    return k;
}

/* Is a platform driver bound to some device (any device link in its directory)? */
static bool platform_driver_bound(const char *drv)
{
    char path[PATH_MAX];
    sfmt(path, sizeof path, "/sys/bus/platform/drivers/%s", drv);
    DIR *dir = opendir(path);
    if (!dir)
        return false;
    bool bound = false;
    struct dirent *e;
    while ((e = readdir(dir)) && !bound)
        bound = e->d_type == DT_LNK && strcmp(e->d_name, "module");
    closedir(dir);
    return bound;
}

/* A Raspberry Pi's display path in words: the KMS (vc4) and render (v3d) cards, or
 * how far vc4 got, and the firmware's simplefb screen while it lasts. */
static void dt_display(const struct bd *b, char *out, size_t cap)
{
    char cards[8][16], drivers[8][32];
    int n = drm_cards(cards, drivers, 8);
    bool vc4 = false;
    size_t l = 0;
    out[0] = 0;
    for (int i = 0; i < n; i++) {
        if (!strncmp(drivers[i], "vc4", 3))
            vc4 = true;
        sfmt(out + l, cap - l, "%s%s (%s)", l ? ", " : "", drivers[i], cards[i]);
        l = strlen(out);
    }
    if (!vc4 && b->pi) {
        const char *st = !exists("/sys/module/vc4") ? "vc4 not loaded"
                         : platform_driver_bound("vc4-drm")
                             ? "vc4 started, waiting for its HDMI and HVS parts (no display device yet)"
                             : "vc4 loaded, not bound to the GPU";
        sfmt(out + l, cap - l, "%s%s", l ? ", " : "", st);
        l = strlen(out);
    }
    char fbname[32];
    if (read_trim("/sys/class/graphics/fb0/name", fbname, sizeof fbname))
        sfmt(out + l, cap - l, "%sfirmware screen /dev/fb0 (%s)", l ? ", " : "", fbname);
}

/* Raspberry Pi power and temperature (as hostd's platform.Health): the rpi_volt
 * hwmon alarm, the SoC zone, cpu0's clock. *uv: under-voltage now. */
static bool pi_power(char *out, size_t cap, bool *uv)
{
    *uv = false;
    out[0] = 0;
    char v[32], path[PATH_MAX], name[32];
    int alarm = -1;
    DIR *dir = opendir("/sys/class/hwmon");
    if (dir) {
        struct dirent *e;
        while ((e = readdir(dir)))
            if (e->d_name[0] != '.') {
                sfmt(path, sizeof path, "/sys/class/hwmon/%s/name", e->d_name);
                if (read_trim(path, name, sizeof name) && !strcmp(name, "rpi_volt")) {
                    sfmt(path, sizeof path, "/sys/class/hwmon/%s/in0_lcrit_alarm", e->d_name);
                    if (read_trim(path, v, sizeof v))
                        alarm = v[0] == '1';
                }
            }
        closedir(dir);
    }
    size_t l = 0;
    if (alarm == 1) {
        *uv = true;
        sfmt(out, cap, "under-voltage now");
    } else if (alarm == 0) {
        sfmt(out, cap, "power OK");
    }
    l = strlen(out);
    if (read_trim("/sys/class/thermal/thermal_zone0/temp", v, sizeof v)) {
        sfmt(out + l, cap - l, "%sSoC %ld C", l ? ", " : "", strtol(v, NULL, 10) / 1000);
        l = strlen(out);
    }
    char cur[32], top[32];
    if (read_trim("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", cur, sizeof cur) &&
        read_trim("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", top, sizeof top))
        sfmt(out + l, cap - l, "%sCPU %.1f of %.1f GHz", l ? ", " : "", strtol(cur, NULL, 10) / 1e6,
             strtol(top, NULL, 10) / 1e6);
    return out[0] != 0;
}

/* Connected outputs with the display's EDID identity (device-tree boards). */
static int outputs_edid(char out[][RB_DIAG_LINE], int max)
{
    DIR *dir = opendir("/sys/class/drm");
    if (!dir)
        return 0;
    int n = 0;
    struct dirent *e;
    while ((e = readdir(dir)) && n < max) {
        if (strncmp(e->d_name, "card", 4) || !strchr(e->d_name, '-'))
            continue;
        char path[PATH_MAX], st[32] = "", modes[128] = "", mode[32] = "";
        sfmt(path, sizeof path, "/sys/class/drm/%s/status", e->d_name);
        if (!read_trim(path, st, sizeof st) || strcmp(st, "connected"))
            continue;
        sfmt(path, sizeof path, "/sys/class/drm/%s/modes", e->d_name);
        if (rb_diag_read_file(path, modes, sizeof modes) > 0)
            sscanf(modes, "%31s", mode);
        char ident[160] = "no EDID";
        uint8_t ed[256];
        sfmt(path, sizeof path, "/sys/class/drm/%s/edid", e->d_name);
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        ssize_t en = fd >= 0 ? read(fd, ed, sizeof ed) : -1;
        if (fd >= 0)
            close(fd);
        if (en > 0)
            bd_edid_ident(ed, (size_t)en, ident, sizeof ident);
        sfmt(out[n++], RB_DIAG_LINE, "Output %s connected%s%s; %s", e->d_name, mode[0] ? ", preferred " : "", mode,
             ident);
    }
    closedir(dir);
    return n;
}

static int drm_connected(char out[][96], int max)
{
    DIR *dir = opendir("/sys/class/drm");
    if (!dir)
        return 0;
    int n = 0;
    struct dirent *e;
    while ((e = readdir(dir)) && n < max) {
        if (strncmp(e->d_name, "card", 4) || !strchr(e->d_name, '-'))
            continue;
        char path[PATH_MAX], st[32] = "", mode[32] = "";
        sfmt(path, sizeof path, "/sys/class/drm/%s/status", e->d_name);
        if (!read_trim(path, st, sizeof st))
            continue;
        sfmt(path, sizeof path, "/sys/class/drm/%s/modes", e->d_name);
        char modes[256] = "";
        if (rb_diag_read_file(path, modes, sizeof modes) > 0)
            sscanf(modes, "%31s", mode);
        sfmt(out[n++], 96, "%s %s%s%s", e->d_name, st, mode[0] ? " " : "", mode);
    }
    closedir(dir);
    return n;
}

/* debugfs, for panel self refresh: systemd mounts it in the real root; mounted
 * read-only here only when it is not (cheap: no device, nothing written). */
static bool debugfs_ready(struct bd *b)
{
    static char mi[64 * 1024];
    if (rb_diag_read_file("/proc/self/mountinfo", mi, sizeof mi) > 0 && strstr(mi, " /sys/kernel/debug "))
        return true;
    if (b->initramfs || b->debugfs_tried || !exists("/sys/kernel/debug"))
        return false;
    b->debugfs_tried = true;
    if (mount("debugfs", "/sys/kernel/debug", "debugfs", MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL) < 0) {
        rb_log("debugfs: %s", strerror(errno));
        return false;
    }
    return true;
}

/* A debugfs file's text on one line ("; " for newlines), or false. */
static bool read_line_joined(const char *path, char *out, size_t cap)
{
    char buf[512];
    if (rb_diag_read_file(path, buf, sizeof buf) <= 0)
        return false;
    trim(buf);
    size_t o = 0;
    for (char *p = buf; *p && o + 3 < cap; p++) {
        if (*p == '\n') {
            out[o++] = ';';
            out[o++] = ' ';
        } else {
            out[o++] = *p;
        }
    }
    out[o] = 0;
    return o > 0;
}

/* The built-in panels (eDP, LVDS, DSI): state and mode, who made them (EDID),
 * and whether panel self refresh runs (amdgpu: psr_state, replay_state;
 * i915: i915_edp_psr_status), "unknown" without debugfs. */
static int panel_lines(struct bd *b, char out[][RB_DIAG_LINE], int max)
{
    DIR *dir = opendir("/sys/class/drm");
    if (!dir)
        return 0;
    int dbg = -1; /* debugfs: asked once, when there is a panel */
    int n = 0;
    struct dirent *e;
    while ((e = readdir(dir)) && n < max) {
        const char *dash = strchr(e->d_name, '-');
        if (strncmp(e->d_name, "card", 4) || !dash ||
            !(strstr(dash, "-eDP-") || strstr(dash, "-LVDS-") || strstr(dash, "-DSI-")))
            continue;
        if (dbg < 0)
            dbg = debugfs_ready(b);
        int card = atoi(e->d_name + 4);
        const char *conn = dash + 1;
        char path[PATH_MAX], st[32] = "", mode[32] = "", modes[128] = "";
        sfmt(path, sizeof path, "/sys/class/drm/%s/status", e->d_name);
        read_trim(path, st, sizeof st);
        sfmt(path, sizeof path, "/sys/class/drm/%s/modes", e->d_name);
        if (rb_diag_read_file(path, modes, sizeof modes) > 0)
            sscanf(modes, "%31s", mode);
        /* EDID: maker, product code, name and text descriptors, size */
        char ident[160] = "no EDID";
        uint8_t ed[256];
        sfmt(path, sizeof path, "/sys/class/drm/%s/edid", e->d_name);
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        ssize_t en = fd >= 0 ? read(fd, ed, sizeof ed) : -1;
        if (fd >= 0)
            close(fd);
        if (en > 0)
            bd_edid_ident(ed, (size_t)en, ident, sizeof ident);
        /* panel self refresh */
        char psr[200] = "", v1[96], v2[96];
        if (!dbg) {
            sfmt(psr, sizeof psr, "unknown (no debugfs)");
        } else {
            sfmt(path, sizeof path, "/sys/kernel/debug/dri/%d/%s/psr_state", card, conn);
            if (read_line_joined(path, v1, sizeof v1)) {
                sfmt(path, sizeof path, "/sys/kernel/debug/dri/%d/%s/psr_capability", card, conn);
                if (!read_line_joined(path, v2, sizeof v2))
                    sfmt(v2, sizeof v2, "?");
                sfmt(psr, sizeof psr, "psr_state %s (0: off), capability: %s", v1, v2);
            }
            sfmt(path, sizeof path, "/sys/kernel/debug/dri/%d/%s/replay_state", card, conn);
            if (read_line_joined(path, v1, sizeof v1)) {
                size_t pl = strlen(psr);
                sfmt(psr + pl, sizeof psr - pl, "%sreplay_state %s", pl ? "; " : "", v1);
            }
            sfmt(path, sizeof path, "/sys/kernel/debug/dri/%d/i915_edp_psr_status", card);
            if (!psr[0] && read_line_joined(path, v1, sizeof v1))
                sfmt(psr, sizeof psr, "%s", v1);
            if (!psr[0])
                sfmt(psr, sizeof psr, "unknown");
        }
        sfmt(out[n++], RB_DIAG_LINE, "Panel %s (card%d): %s%s%s; %s; self refresh: %s", conn, card, st,
             mode[0] ? " " : "", mode, ident, psr);
    }
    closedir(dir);
    return n;
}

/* The display drivers' options that matter for a frozen panel. */
static bool driver_options(char *out, size_t cap)
{
    char v[32];
    out[0] = 0;
    if (read_trim("/sys/module/amdgpu/parameters/dcdebugmask", v, sizeof v)) {
        unsigned long m = strtoul(v, NULL, 0);
        char what[128];
        bd_dcdebugmask_text(m, what, sizeof what);
        sfmt(out, cap, "amdgpu.dcdebugmask=0x%lx (%s)", m, what);
    }
    if (read_trim("/sys/module/i915/parameters/enable_psr", v, sizeof v)) {
        size_t l = strlen(out);
        sfmt(out + l, cap - l, "%si915.enable_psr=%s", l ? "; " : "", v);
    }
    return out[0] != 0;
}

static bool platform_fb_exists(char *which, size_t cap)
{
    static const char *const names[] = {FB_PLATFORM_DEVS};
    for (size_t i = 0; i < sizeof names / sizeof names[0]; i++) {
        char path[128];
        sfmt(path, sizeof path, "/sys/bus/platform/devices/%s", names[i]);
        if (exists(path)) {
            if (which)
                sfmt(which, cap, "%s", names[i]);
            return true;
        }
    }
    return false;
}

static void keyboards(struct bd *b, char *out, size_t cap)
{
    out[0] = 0;
    size_t n = 0;
    for (int i = 0; i < MAXIN; i++)
        if (b->in[i].fd >= 0 && n < cap) {
            int k = snprintf(out + n, cap - n, "%s%s", n ? "; " : "", b->in[i].name);
            n = k > 0 && n + (size_t)k < cap ? n + (size_t)k : cap - 1;
        }
}

/* ------------------------------------------------------------------------ */
/* kernel log                                                               */

static void add_problem(struct bd *b, int score, uint64_t ms, const char *text)
{
    for (int i = 0; i < b->nprob; i++)
        if (!strcmp(b->prob[i].text, text)) {
            b->prob[i].count++;
            if (score > b->prob[i].score)
                b->prob[i].score = score;
            return;
        }
    int slot = b->nprob < MAXPROB ? b->nprob++ : -1;
    if (slot < 0) {
        int low = 0;
        for (int i = 1; i < MAXPROB; i++)
            if (b->prob[i].score < b->prob[low].score)
                low = i;
        if (b->prob[low].score >= score)
            return;
        slot = low;
    }
    struct problem *p = &b->prob[slot];
    p->score = score;
    p->ms = ms;
    p->count = 1;
    sfmt(p->text, sizeof p->text, "%s", text);
    b->dirty = true;
}

static void note_probe_failure(struct bd *b, const char *t)
{
    /* "drv 0000:c4:00.0: probe with driver drv failed with error -2" or
     * "drv: probe of 0000:c4:00.0 failed with error -2" */
    char addr[16] = "", drv[32] = "";
    int err = 0;
    const char *p;
    if ((p = strstr(t, "probe of ")) && sscanf(p + 9, "%15s failed with error %d", addr, &err) == 2) {
        sscanf(t, "%31[^:]", drv);
    } else if ((p = strstr(t, "probe with driver ")) && sscanf(p + 18, "%31s failed with error %d", drv, &err) == 2) {
        sscanf(t, "%*s %15[^:]", addr);
    } else {
        return;
    }
    if (strlen(addr) < 7 || addr[4] != ':')
        return;
    for (int i = 0; i < b->nprobe_fail; i++)
        if (!strcmp(b->probe_fail[i].addr, addr)) {
            sfmt(b->probe_fail[i].text, sizeof b->probe_fail[i].text, "%s failed to start (error %d)", drv, err);
            return;
        }
    if (b->nprobe_fail < MAXPCI) {
        sfmt(b->probe_fail[b->nprobe_fail].addr, 16, "%s", addr);
        sfmt(b->probe_fail[b->nprobe_fail].text, 96, "%s failed to start (error %d)", drv, err);
        b->nprobe_fail++;
    }
}

static void stage_done(struct bd *b, int s, uint64_t ms, const char *detail)
{
    if (s < 0 || s >= BD_NSTAGES)
        return;
    if (!b->st.done[s]) {
        b->st.done[s] = true;
        b->st.failed[s] = false;
        b->st.ms[s] = ms;
        if (detail && detail[0])
            sfmt(b->st.detail[s], sizeof b->st.detail[s], "%s", detail);
        b->dirty = true;
        if (!b->initramfs) /* the kernel log has RetroBoot's own stage notes */
            rb_log("stage %s at %llu ms%s%s", bd_stage_id(s), (unsigned long long)ms, detail && *detail ? ": " : "",
                   detail ? detail : "");
    }
}

static void stage_fail(struct bd *b, int s, const char *detail)
{
    if (b->st.done[s])
        return;
    if (!b->st.failed[s] || strcmp(b->st.detail[s], detail)) {
        b->st.failed[s] = true;
        sfmt(b->st.detail[s], sizeof b->st.detail[s], "%s", detail);
        b->dirty = true;
    }
}

static void kmsg_take(struct bd *b, const struct bd_kmsg *k)
{
    if (!strncmp(k->text, "rb-bootdiag:", 12))
        return;
    uint64_t ms = k->ts_us / 1000;
    char id[32], detail[160];
    if (bd_marker(k->text, id, sizeof id, detail, sizeof detail)) {
        if (!strcmp(id, "panic")) {
            char t[RB_DIAG_LINE];
            sfmt(t, sizeof t, "Startup stopped: %s", detail);
            add_problem(b, 100, ms, t);
            int s = !b->st.done[BD_MEDIUM_FOUND] ? BD_MEDIUM_FOUND : !b->st.done[BD_ROOT] ? BD_ROOT : BD_SWITCH_ROOT;
            stage_fail(b, s, detail);
        } else {
            stage_done(b, bd_stage_from_id(id), ms, detail);
            if (!strcmp(id, "medium-found") && detail[0])
                sfmt(b->medium, sizeof b->medium, "%s", detail);
        }
    }
    int score = bd_problem_score(k->prio, k->text);
    if (score > 0 && k->seq > b->seq_known)
        add_problem(b, score, ms, k->text);
    if (score >= 85)
        note_probe_failure(b, k->text);
    b->ring[b->ring_head] = *k;
    b->ring_head = (b->ring_head + 1) % RING;
    if (b->ring_n < RING)
        b->ring_n++;
    if (k->prio <= 6)
        b->dirty = true;
}

static void kmsg_read(void *data, int fd, uint32_t events)
{
    (void)events;
    struct bd *b = data;
    char rec[8192];
    for (;;) {
        ssize_t n = read(fd, rec, sizeof rec - 1);
        if (n < 0) {
            if (errno == EINTR || errno == EPIPE) /* EPIPE: records were overwritten; go on */
                continue;
            break;
        }
        if (n == 0)
            break;
        rec[n] = 0;
        struct bd_kmsg k;
        if (bd_kmsg_parse(rec, (size_t)n, &k))
            continue;
        b->kmsg_seq = k.seq;
        kmsg_take(b, &k);
    }
}

/* ------------------------------------------------------------------------ */
/* presenter state                                                          */

static void presenter_read(struct bd *b)
{
    char buf[2048];
    struct prstate p;
    memset(&p, 0, sizeof p);
    if (rb_diag_read_file(RB_DIAG_PRESENTER, buf, sizeof buf) > 0) {
        p.present = true;
        char *save = NULL;
        for (char *l = strtok_r(buf, "\n", &save); l; l = strtok_r(NULL, "\n", &save)) {
            char *eq = strchr(l, '=');
            if (!eq)
                continue;
            *eq = 0;
            const char *k = l, *v = eq + 1;
            if (!strcmp(k, "pid"))
                p.pid = atoi(v);
            else if (!strcmp(k, "input"))
                p.input = atoi(v) != 0;
            else if (!strcmp(k, "display"))
                p.display = atoi(v) != 0;
            else if (!strcmp(k, "hostd"))
                p.hostd = atoi(v) != 0;
            else if (!strcmp(k, "visible"))
                p.visible = atoi(v) != 0;
            else if (!strcmp(k, "scene"))
                sfmt(p.scene, sizeof p.scene, "%s", v);
            else if (!strcmp(k, "view"))
                sfmt(p.view, sizeof p.view, "%s", v);
            else if (!strcmp(k, "driver"))
                sfmt(p.driver, sizeof p.driver, "%s", v);
            else if (!strcmp(k, "device"))
                sfmt(p.device, sizeof p.device, "%s", v);
            else if (!strcmp(k, "mode"))
                sfmt(p.mode, sizeof p.mode, "%s", v);
            else if (!strcmp(k, "start_ms"))
                p.start_ms = strtoull(v, NULL, 10);
            else if (!strcmp(k, "input_ms"))
                p.input_ms = strtoull(v, NULL, 10);
            else if (!strcmp(k, "view_ms"))
                p.view_ms = strtoull(v, NULL, 10);
        }
        p.alive = p.pid > 0 && (kill(p.pid, 0) == 0 || errno == EPERM);
        struct stat stt;
        if (stat(RB_DIAG_PRESENTER, &stt) == 0) {
            long age = (long)(time(NULL) - stt.st_mtime);
            p.age_s = age > 0 ? age : 0;
        }
    }
    long age = p.age_s;
    p.age_s = b->pr.age_s;
    if (memcmp(&p, &b->pr, sizeof p))
        b->dirty = true;
    p.age_s = age;
    b->pr = p;
    for (int i = 0; i < 2; i++) {
        struct flipinfo f;
        memset(&f, 0, sizeof f);
        char path[96], fb[512];
        sfmt(path, sizeof path, "%s/flips-%s", RB_DIAG_DIR, i ? "splash" : "presenter");
        if (rb_diag_read_file(path, fb, sizeof fb) > 0) {
            f.present = true;
            char *sv = NULL;
            for (char *l = strtok_r(fb, "\n", &sv); l; l = strtok_r(NULL, "\n", &sv)) {
                unsigned long long v;
                if (sscanf(l, "stalls=%llu", &v) == 1)
                    f.stalls = (unsigned)v;
                else if (sscanf(l, "slow_commits=%llu", &v) == 1)
                    f.slow = (unsigned)v;
                else if (sscanf(l, "first_ms=%llu", &v) == 1)
                    f.first_ms = v;
                else if (sscanf(l, "last_ms=%llu", &v) == 1)
                    f.last_ms = v;
                else if (sscanf(l, "slow_max_ms=%llu", &v) == 1)
                    f.slow_max = v;
                else if (!strncmp(l, "connector=", 10))
                    sfmt(f.conn, sizeof f.conn, "%s", l + 10);
                else if (!strncmp(l, "driver=", 7))
                    sfmt(f.drv, sizeof f.drv, "%s", l + 7);
            }
        }
        if (memcmp(&f, &b->flips[i], sizeof f))
            b->dirty = true;
        b->flips[i] = f;
    }
}

/* The presenter decides about Escape while it has input and its loop runs
 * (heartbeat); stuck in the kernel, rb-bootdiag takes Escape again, so the
 * report is still saved when the view is asked for. */
static bool presenter_owns_input(const struct bd *b)
{
    return b->pr.present && b->pr.alive && b->pr.input && b->pr.age_s < 5;
}

/* "Display updates are stalling ..." from the drawers' counts; empty when none. */
static bool flips_problem(const struct bd *b, char *t, size_t cap)
{
    unsigned stalls = 0, slow = 0;
    uint64_t first = 0, slow_max = 0;
    const char *conn = "", *drv = "";
    for (int i = 0; i < 2; i++) {
        const struct flipinfo *f = &b->flips[i];
        stalls += f->stalls;
        slow += f->slow;
        if (f->first_ms && (!first || f->first_ms < first))
            first = f->first_ms;
        if (f->slow_max > slow_max)
            slow_max = f->slow_max;
        if (f->conn[0] && !conn[0]) {
            conn = f->conn;
            drv = f->drv;
        }
    }
    if (!stalls && !slow)
        return false;
    char tm[16] = "", sm[16] = "";
    if (first)
        bd_fmt_secs(first, tm, sizeof tm);
    bd_fmt_secs(slow_max, sm, sizeof sm);
    if (stalls)
        sfmt(t, cap, "Display updates are stalling (page flip timeouts): %u on %s (%s) since %s%s%s%s", stalls, conn,
             drv, tm, slow ? "; a display change waited " : "", slow ? sm : "", "");
    else
        sfmt(t, cap, "A display change waited %s (%s, %s): the display is slow to take new frames", sm, conn, drv);
    return true;
}

static bool ready_now(const struct bd *b)
{
    if (!b->pr.alive || !b->pr.display)
        return false;
    return b->cfg.installer ? !strcmp(b->pr.view, "panel") : !strcmp(b->pr.view, "guest");
}

/* ------------------------------------------------------------------------ */
/* stages                                                                   */

static const char *stage_label(const struct bd *b, int s, char *buf, size_t cap)
{
    const char *d = b->st.detail[s];
    switch (s) {
    case BD_KERNEL: return "Startup system loaded";
    case BD_MEDIUM_SEARCH: return "Looking for the installer disk";
    case BD_MEDIUM_FOUND:
        if (b->st.done[s] && d[0]) {
            sfmt(buf, cap, "Installer disk found (%s)", d);
            return buf;
        }
        return b->st.failed[s] ? "Installer disk not found" : "Installer disk found";
    case BD_SQUASHFS: return "Installer files opened";
    case BD_ROOT: return b->cfg.installer ? "Installer system ready" : "Startup disk ready";
    case BD_SWITCH_ROOT: return b->cfg.installer ? "Installer system started" : "System started";
    case BD_DISPLAY:
        if (d[0]) {
            sfmt(buf, cap, "Display driver: %s", d);
            return buf;
        }
        return "Display driver";
    case BD_PRESENTER: return "RetroBoot screen";
    case BD_HOSTD: return "RetroBoot service";
    case BD_READY: return b->cfg.installer ? "Installer window shown" : "Mac OS shown";
    }
    return "?";
}

static const char *progress_text(const struct bd *b, int s)
{
    switch (s) {
    case BD_KERNEL: return "Starting up";
    case BD_MEDIUM_SEARCH: return "Looking for the installer disk";
    case BD_MEDIUM_FOUND: return "Installer disk found";
    case BD_ROOT: return b->cfg.installer ? "Opening the installer" : "Starting up";
    case BD_SWITCH_ROOT: return b->cfg.installer ? "Starting the installer" : "Starting up";
    case BD_DISPLAY: return "Starting the display";
    case BD_PRESENTER: return "Starting RetroBoot";
    case BD_HOSTD: return b->cfg.installer ? "Starting the installer" : "Starting Mac OS";
    case BD_READY: return "Ready";
    }
    return "";
}

static void display_eval(struct bd *b, uint64_t now)
{
    char cards[4][16], drivers[4][32];
    int n = kms_cards(cards, drivers, 4);
    if (n > 0) {
        char d[96];
        sfmt(d, sizeof d, "%s (%s)", drivers[0], cards[0]);
        if (b->pr.alive && b->pr.driver[0] && b->pr.display)
            sfmt(d, sizeof d, "%s on %s %s", b->pr.driver, b->pr.device, b->pr.mode);
        stage_done(b, BD_DISPLAY, now, d);
        return;
    }
    if (b->st.done[BD_DISPLAY])
        return;
    /* nothing yet: is it a failure? */
    pci_scan(b);
    const struct pcidev *dev = NULL;
    for (int i = 0; i < b->npci; i++)
        if (!dev || b->pci[i].boot_vga || b->pci[i].has_fb)
            dev = &b->pci[i];
    bool late = b->st.done[BD_SWITCH_ROOT] && now > b->st.ms[BD_SWITCH_ROOT] + DISPLAY_GRACE_MS;
    if (!dev) {
        if (late && b->dt) {
            char d[96], st[200];
            dt_display(b, st, sizeof st);
            sfmt(d, sizeof d, "%s", st);
            stage_fail(b, BD_DISPLAY, d);
        } else if (late) {
            stage_fail(b, BD_DISPLAY, "no display adapter found");
        }
        return;
    }
    if (dev->driver[0])
        return; /* bound or still probing */
    if (dev->probe_err[0] || late) {
        char d[96];
        const char *ven = bd_pci_vendor(dev->vendor);
        if (dev->probe_err[0])
            sfmt(d, sizeof d, "%s (%s %04x:%04x)", dev->probe_err, ven ? ven : "display", dev->vendor, dev->device);
        else
            sfmt(d, sizeof d, "none for %s %04x:%04x", ven ? ven : "display", dev->vendor, dev->device);
        stage_fail(b, BD_DISPLAY, d);
    }
}

static void progress_publish(struct bd *b)
{
    int stage;
    int pm = bd_progress(&b->st, &stage);
    char line[160];
    bd_progress_line(pm, stage, progress_text(b, stage), line, sizeof line);
    if (strcmp(line, b->pub_line)) {
        if (rb_diag_write_atomic(RB_DIAG_PROGRESS, line, strlen(line)) == 0) {
            sfmt(b->pub_line, sizeof b->pub_line, "%s", line);
            b->st.published = pm;
        }
    }
}

static void stages_eval(struct bd *b)
{
    uint64_t now = now_ms();
    if (!b->cfg.installer) {
        b->st.skipped[BD_MEDIUM_SEARCH] = b->st.skipped[BD_MEDIUM_FOUND] = b->st.skipped[BD_SQUASHFS] = true;
    }
    if (!b->initramfs) {
        stage_done(b, BD_SWITCH_ROOT, now, NULL);
        if (!b->cfg.installer)
            stage_done(b, BD_ROOT, b->st.ms[BD_SWITCH_ROOT], NULL);
    }
    presenter_read(b);
    display_eval(b, now);
    if (b->pr.alive)
        stage_done(b, BD_PRESENTER, b->pr.start_ms ? b->pr.start_ms : now, NULL);
    if (b->pr.alive && b->pr.hostd)
        stage_done(b, BD_HOSTD, now, NULL);
    if (ready_now(b))
        stage_done(b, BD_READY, b->pr.view_ms ? b->pr.view_ms : now, NULL);
    progress_publish(b);
}

/* ------------------------------------------------------------------------ */
/* the view                                                                 */

static int prob_cmp(const void *a, const void *b)
{
    const struct problem *x = *(const struct problem *const *)a, *y = *(const struct problem *const *)b;
    if (x->score != y->score)
        return y->score - x->score;
    return x->ms < y->ms ? -1 : x->ms > y->ms;
}

static int sorted_problems(struct bd *b, const struct problem **out, int max, int min_score)
{
    const struct problem *all[MAXPROB];
    int n = 0;
    for (int i = 0; i < b->nprob; i++)
        if (b->prob[i].score >= min_score)
            all[n++] = &b->prob[i];
    qsort(all, (size_t)n, sizeof all[0], prob_cmp);
    if (n > max)
        n = max;
    memcpy(out, all, sizeof all[0] * (size_t)n);
    return n;
}

/* USB mass-storage devices without a driver, 10 s after power-on (a stick the
 * installer cannot read: usb-storage/uas not loaded, ARCHITECTURE 16). */
static void usb_storage_eval(struct bd *b, uint64_t now)
{
    if (now < 10000) {
        b->nusbstor = 0;
        return;
    }
    b->nusbstor = bd_usb_storage_unbound("/sys/bus/usb/devices", b->usbstor, 4);
}

static void view_build(struct bd *b, struct rb_diag_doc *d)
{
    uint64_t now = now_ms();
    char t[RB_DIAG_LINE], tm[16];
    rb_diag_doc_init(d, "Startup Details");
    sfmt(t, sizeof t, "%s%s", b->build, b->cfg.installer ? " \xe2\x80\x94 Installer" : "");
    rb_diag_doc_add(d, 'H', t);
    if (b->dt)
        sfmt(t, sizeof t, "Computer: %s%s%s", b->dmi[0], b->dmi[1][0] ? " \xc2\xb7 " : "", b->dmi[1]);
    else
        sfmt(t, sizeof t, "Computer: %s%s%s", b->dmi[0], b->dmi[1][0] ? " \xc2\xb7 BIOS " : "", b->dmi[1]);
    rb_diag_doc_add(d, 'H', t);
    bd_fmt_secs(now, tm, sizeof tm);
    sfmt(t, sizeof t, "Processor: %s \xc2\xb7 Memory: %s \xc2\xb7 %s since power-on", b->cpu[0] ? b->cpu : "?",
             b->mem, tm);
    rb_diag_doc_add(d, 'H', t);

    /* checklist: the first stage not reached is "waiting", later ones too */
    for (int s = 0; s < BD_NSTAGES; s++) {
        if (b->st.skipped[s])
            continue;
        char lb[128];
        const char *label = stage_label(b, s, lb, sizeof lb);
        if (b->st.done[s]) {
            bd_fmt_secs(b->st.ms[s], tm, sizeof tm);
            rb_diag_doc_stage(d, RB_DIAG_OK, tm, label);
        } else if (b->st.failed[s]) {
            rb_diag_doc_stage(d, RB_DIAG_FAIL, "", label);
        } else {
            rb_diag_doc_stage(d, RB_DIAG_WAIT, "", label);
        }
    }

    /* problems: the synthesized ones first */
    if (flips_problem(b, t, sizeof t))
        rb_diag_doc_add(d, 'P', t);
    if (b->pr.alive && b->pr.age_s >= 10) {
        sfmt(t, sizeof t, "The RetroBoot screen has not responded for %ld s (waiting for the display driver?)",
             b->pr.age_s);
        rb_diag_doc_add(d, 'P', t);
    }
    if (b->st.failed[BD_DISPLAY]) {
        sfmt(t, sizeof t, "No display driver started: %s", b->st.detail[BD_DISPLAY]);
        rb_diag_doc_add(d, 'P', t);
    }
    if (b->failed_units[0]) {
        sfmt(t, sizeof t, "Failed services: %s", b->failed_units);
        rb_diag_doc_add(d, 'P', t);
    }
    usb_storage_eval(b, now);
    for (int i = 0; i < b->nusbstor; i++) {
        sfmt(t, sizeof t, "USB storage %s has no driver (usb-storage and uas not loaded): RetroBoot cannot read it",
             b->usbstor[i]);
        rb_diag_doc_add(d, 'P', t);
    }
    char pw[160];
    bool uv = false;
    if (b->pi && pi_power(pw, sizeof pw, &uv) && uv) {
        sfmt(t, sizeof t, "Under-voltage: the power supply is too weak for this Raspberry Pi (%s)", pw);
        rb_diag_doc_add(d, 'P', t);
    }
    const struct problem *pp[16];
    int np = sorted_problems(b, pp, 16, 40);
    for (int i = 0; i < np; i++) {
        bd_fmt_secs(pp[i]->ms, tm, sizeof tm);
        if (pp[i]->count > 1)
            sfmt(t, sizeof t, "%s  %s (%d times)", tm, pp[i]->text, pp[i]->count);
        else
            sfmt(t, sizeof t, "%s  %s", tm, pp[i]->text);
        rb_diag_doc_add(d, 'P', t);
    }

    /* display and keyboard */
    pci_scan(b);
    for (int i = 0; i < b->npci; i++) {
        struct pcidev *p = &b->pci[i];
        const char *ven = bd_pci_vendor(p->vendor);
        pci_expect(p);
        char drv[96];
        if (p->driver[0])
            sfmt(drv, sizeof drv, "driver %s", p->driver);
        else if (p->probe_err[0])
            sfmt(drv, sizeof drv, "no driver (%s)", p->probe_err);
        else if (p->expect[0] && blocked(p->expect))
            sfmt(drv, sizeof drv, "no driver (%s blocked by modprobe.blacklist)", p->expect);
        else if (p->expect[0])
            sfmt(drv, sizeof drv, "no driver (%s not loaded)", p->expect);
        else
            sfmt(drv, sizeof drv, "no driver available");
        sfmt(t, sizeof t, "%s %04x:%04x at %s: %s%s", ven ? ven : "Display", p->vendor, p->device, p->addr, drv,
                 p->boot_vga || p->has_fb ? " (startup display)" : "");
        rb_diag_doc_add(d, 'D', t);
    }
    if (b->dt) {
        char dd[200];
        dt_display(b, dd, sizeof dd);
        sfmt(t, sizeof t, "Display: %s", dd);
        rb_diag_doc_add(d, 'D', t);
        static char outs[4][RB_DIAG_LINE];
        int no = outputs_edid(outs, 4);
        for (int i = 0; i < no; i++)
            rb_diag_doc_add(d, 'D', outs[i]);
        if (b->pi && pi_power(pw, sizeof pw, &uv)) {
            sfmt(t, sizeof t, "Power and temperature: %s", pw);
            rb_diag_doc_add(d, 'D', t);
        }
    }
    char conn[4][96];
    int nc = b->dt ? 0 : drm_connected(conn, 4);
    for (int i = 0; i < nc; i++) {
        sfmt(t, sizeof t, "Output %s", conn[i]);
        rb_diag_doc_add(d, 'D', t);
    }
    static char panels[4][RB_DIAG_LINE];
    int npanel = panel_lines(b, panels, 4);
    for (int i = 0; i < npanel; i++)
        rb_diag_doc_add(d, 'D', panels[i]);
    char opts[200];
    if (driver_options(opts, sizeof opts)) {
        sfmt(t, sizeof t, "Display driver options: %s", opts);
        rb_diag_doc_add(d, 'D', t);
    }
    if (b->fi_ok && b->fb_state[0]) {
        sfmt(t, sizeof t, "Firmware screen %dx%d: %s", b->fi.w, b->fi.h, b->fb_state);
        rb_diag_doc_add(d, 'D', t);
    }
    char kb[400];
    keyboards(b, kb, sizeof kb);
    sfmt(t, sizeof t, "Keyboards: %s", kb[0] ? kb : "none found yet");
    rb_diag_doc_add(d, 'D', t);

    /* latest kernel messages (not RetroBoot's debug notes), oldest first */
    int pick[40], np2 = 0;
    for (int i = 0; i < b->ring_n && np2 < 40; i++) {
        int idx = (b->ring_head - 1 - i + RING) % RING;
        const struct bd_kmsg *k = &b->ring[idx];
        if (k->prio > 6 && strncmp(k->text, "retroboot:", 10))
            continue;
        if (k->prio >= 5 && !strncmp(k->text, "systemd[1]: ", 12)) /* unit progress: in the report only */
            continue;
        pick[np2++] = idx;
    }
    for (int i = np2 - 1; i >= 0; i--) {
        const struct bd_kmsg *m = &b->ring[pick[i]];
        sfmt(t, sizeof t, "[%5llu.%03llu] %s", (unsigned long long)(m->ts_us / 1000000),
                 (unsigned long long)(m->ts_us / 1000 % 1000), m->text);
        rb_diag_doc_add(d, 'L', t);
    }

    if (b->save_busy && !b->saved_where[0])
        sfmt(t, sizeof t, "The report is saved on the USB stick as soon as the installer has finished reading it.");
    else if (b->save_pid > 0 && !b->saved_where[0])
        sfmt(t, sizeof t, "Saving the report\xe2\x80\xa6");
    else if (b->saved_where[0])
        sfmt(t, sizeof t, "Saved %s", b->saved_where);
    else if (b->save_err[0])
        sfmt(t, sizeof t, "Not saved: %s", b->save_err);
    else
        sfmt(t, sizeof t, "The report is saved when this window opens.");
    rb_diag_doc_add(d, 'F', t);
}

static void fb_draw(struct bd *b);

/* Rebuild the view; write it when it changed (at most once a second unless forced). */
static void view_update(struct bd *b, bool force)
{
    uint64_t now = now_ms();
    if (!force && !b->dirty)
        return;
    if (!force && now - b->view_written_ms < (b->visible ? 1000u : 4000u))
        return;
    b->dirty = false;
    static struct rb_diag_doc nd;
    view_build(b, &nd);
    if (memcmp(&nd, &b->doc, sizeof nd)) {
        b->doc = nd;
        b->doc_gen++;
        size_t n = rb_diag_doc_format(&b->doc, b->doc_text, 64 * 1024);
        rb_diag_write_atomic(RB_DIAG_VIEW, b->doc_text, n);
    }
    b->view_written_ms = now;
    if (b->visible)
        fb_draw(b);
}

/* ------------------------------------------------------------------------ */
/* firmware framebuffer                                                     */

/* A device-tree board's firmware screen: /dev/fb0 of simplefb (the Raspberry Pi kernel
 * has simplefb, not simpledrm; vc4 removes it when it takes over) or of bcm2708_fb (the
 * firmware's framebuffer without the KMS overlay; QEMU's raspi4b). */
static void fb_setup_fbdev(struct bd *b)
{
    int fd = open("/dev/fb0", O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        rb_log("no firmware framebuffer (no boot_params, no /dev/fb0)");
        return;
    }
    struct fb_fix_screeninfo fix;
    struct fb_var_screeninfo var;
    bool ok = ioctl(fd, FBIOGET_FSCREENINFO, &fix) == 0 && ioctl(fd, FBIOGET_VSCREENINFO, &var) == 0;
    close(fd);
    if (!ok || !bd_firmware_fbdev(fix.id)) {
        rb_log("/dev/fb0 is not a firmware framebuffer");
        return;
    }
    struct bd_fbdev v = {.w = (int)var.xres, .h = (int)var.yres, .bpp = (int)var.bits_per_pixel,
                         .pitch = (int)fix.line_length, .roff = (uint8_t)var.red.offset,
                         .rlen = (uint8_t)var.red.length, .goff = (uint8_t)var.green.offset,
                         .glen = (uint8_t)var.green.length, .boff = (uint8_t)var.blue.offset,
                         .blen = (uint8_t)var.blue.length, .base = fix.smem_start};
    b->fi_ok = bd_fbdev_info(&v, &b->fi) == 0;
    b->fbdev = b->fi_ok;
    if (b->fi_ok)
        rb_log("firmware framebuffer /dev/fb0 (%.16s) %dx%d %d bpp pitch %d", fix.id, b->fi.w, b->fi.h,
               b->fi.depth, b->fi.pitch);
}

static void fb_setup(struct bd *b)
{
    uint8_t buf[0x40];
    int fd = open("/sys/kernel/boot_params/data", O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        if (b->dt)
            fb_setup_fbdev(b);
        return;
    }
    ssize_t n = read(fd, buf, sizeof buf);
    close(fd);
    b->fi_ok = n > 0 && bd_screen_info(buf, (size_t)n, &b->fi) == 0;
    if (b->fi_ok)
        rb_log("firmware framebuffer %dx%d %d bpp pitch %d at 0x%llx (%s)", b->fi.w, b->fi.h, b->fi.depth,
               b->fi.pitch, (unsigned long long)b->fi.base, b->fi.type == 0x70 ? "EFI" : "VESA");
    else
        rb_log("no firmware framebuffer described in boot_params");
}

static int boot_display_driver(struct bd *b)
{
    pci_scan(b);
    if (b->fb_bar_pci < 0)
        return -1;
    return b->pci[b->fb_bar_pci].driver[0] ? 1 : 0;
}

/* May rb-bootdiag write to the firmware framebuffer now? Only while no
 * kernel driver owns the display: the firmware's framebuffer device still
 * exists (a KMS driver removes it before it uses the memory), or it is gone
 * but the display adapter has had no driver for FB_SETTLE_MS (a driver
 * started and failed; nothing uses the memory), and never with a DRM device. */
static bool fb_allowed(struct bd *b, char *why, size_t cap)
{
    if (!b->cfg.fb || !b->fi_ok) {
        sfmt(why, cap, "not used");
        return false;
    }
    char cards[4][16], drivers[4][32];
    if (kms_cards(cards, drivers, 4) > 0) {
        sfmt(why, cap, "taken over by %s", drivers[0]);
        b->fb_nodrv_since = 0;
        return false;
    }
    if (b->fbdev) {
        /* simplefb's /dev/fb0 exists only until a KMS driver (vc4) removes it */
        char name[32];
        if (read_trim("/sys/class/graphics/fb0/name", name, sizeof name) && bd_firmware_fbdev(name)) {
            sfmt(why, cap, "no display driver; shown by RetroBoot directly");
            return true;
        }
        sfmt(why, cap, "released by a display driver");
        return false;
    }
    int drv = boot_display_driver(b);
    if (drv == 1) {
        sfmt(why, cap, "%s is starting", b->pci[b->fb_bar_pci].driver);
        b->fb_nodrv_since = 0;
        return false;
    }
    char plat[32];
    if (platform_fb_exists(plat, sizeof plat)) {
        sfmt(why, cap, "no display driver; shown by RetroBoot directly");
        return true;
    }
    if (drv < 0) {
        sfmt(why, cap, "released (adapter unknown)");
        return false;
    }
    uint64_t now = now_ms();
    if (!b->fb_nodrv_since)
        b->fb_nodrv_since = now;
    if (now - b->fb_nodrv_since < FB_SETTLE_MS) {
        sfmt(why, cap, "released by a display driver");
        return false;
    }
    sfmt(why, cap, "display driver failed; shown by RetroBoot directly");
    return true;
}

static int fb_map(struct bd *b)
{
    if (b->fb)
        return 0;
    b->fb_len = (size_t)b->fi.pitch * (size_t)b->fi.h;
    long pg = sysconf(_SC_PAGESIZE);
    uint64_t base = b->fi.base & ~(uint64_t)(pg - 1);
    b->fb_off = (size_t)(b->fi.base - base);
    b->fb_maplen = (b->fb_len + b->fb_off + (size_t)pg - 1) & ~(size_t)(pg - 1);
    if (b->fbdev) {
        b->fb_off = 0;
        b->fb_maplen = (b->fb_len + (size_t)pg - 1) & ~(size_t)(pg - 1);
        int fd = open("/dev/fb0", O_RDWR | O_CLOEXEC);
        if (fd < 0) {
            rb_log("/dev/fb0: %s", strerror(errno));
            return -1;
        }
        void *m = mmap(NULL, b->fb_maplen, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        close(fd);
        if (m == MAP_FAILED) {
            rb_log("mmap /dev/fb0: %s", strerror(errno));
            return -1;
        }
        b->fb = m;
        rb_log("framebuffer mapped through /dev/fb0");
        return 0;
    }
    /* The display adapter's BAR through sysfs (write-combined when the BAR
     * allows it), else /dev/mem (uncached). */
    if (b->fb_bar_pci >= 0) {
        const struct pcidev *d = &b->pci[b->fb_bar_pci];
        char path[PATH_MAX];
        sfmt(path, sizeof path, "/sys/bus/pci/devices/%s/resource", d->addr);
        FILE *f = fopen(path, "re");
        int bar = -1, i = 0;
        unsigned long long s = 0, e, fl, bs = 0;
        while (f && fscanf(f, "%llx %llx %llx", &s, &e, &fl) == 3 && i < 6) {
            if (s && b->fi.base >= s && b->fi.base <= e && bar < 0) {
                bar = i;
                bs = s;
            }
            i++;
        }
        if (f)
            fclose(f);
        for (int wc = 1; bar >= 0 && wc >= 0; wc--) {
            sfmt(path, sizeof path, "/sys/bus/pci/devices/%s/resource%d%s", d->addr, bar, wc ? "_wc" : "");
            int fd = open(path, O_RDWR | O_CLOEXEC);
            if (fd < 0)
                continue;
            uint64_t off = base - bs;
            void *m = mmap(NULL, b->fb_maplen, PROT_READ | PROT_WRITE, MAP_SHARED, fd, (off_t)off);
            close(fd);
            if (m != MAP_FAILED) {
                b->fb = m;
                rb_log("framebuffer mapped through %s", path);
                return 0;
            }
            rb_log("mmap %s: %s", path, strerror(errno));
        }
    }
    int fd = open("/dev/mem", O_RDWR | O_SYNC | O_CLOEXEC);
    if (fd < 0) {
        rb_log("/dev/mem: %s", strerror(errno));
        return -1;
    }
    void *m = mmap(NULL, b->fb_maplen, PROT_READ | PROT_WRITE, MAP_SHARED, fd, (off_t)base);
    close(fd);
    if (m == MAP_FAILED) {
        rb_log("mmap /dev/mem at 0x%llx: %s", (unsigned long long)base, strerror(errno));
        return -1;
    }
    b->fb = m;
    rb_log("framebuffer mapped through /dev/mem");
    return 0;
}

static void fb_unmap(struct bd *b)
{
    if (b->fb)
        munmap(b->fb, b->fb_maplen);
    b->fb = NULL;
}

/* Write the rows of `want` (native format, pitch * h) that differ from what is shown. */
static void fb_write(struct bd *b, const uint8_t *want)
{
    uint8_t *fb = b->fb + b->fb_off;
    size_t rowb = (size_t)b->fi.w * (size_t)(b->fi.depth / 8);
    for (int y = 0; y < b->fi.h; y++) {
        size_t o = (size_t)y * (size_t)b->fi.pitch;
        if (b->shown_ok && !memcmp(b->shown + o, want + o, rowb))
            continue;
        memcpy(fb + o, want + o, rowb);
        memcpy(b->shown + o, want + o, rowb);
    }
    b->shown_ok = true;
}

static void fb_draw(struct bd *b)
{
    char why[96];
    bool ok = fb_allowed(b, why, sizeof why);
    bool changed = ok != b->fb_was_allowed;
    b->fb_was_allowed = ok;
    if (strcmp(why, b->fb_state)) {
        sfmt(b->fb_state, sizeof b->fb_state, "%s", why);
        b->dirty = true;
        if (!b->initramfs)
            rb_log("firmware framebuffer: %s", why);
    }
    if (!ok) {
        if (b->fb) {
            fb_unmap(b); /* never touch the memory of a driver that took it */
            b->shown_ok = false;
        }
        return;
    }
    if (changed)
        b->shown_ok = false; /* regained: what is on screen is unknown, write everything */
    int want = b->visible ? 2 : b->hint ? 1 : 0;
    if (!want && !b->fb_touched)
        return;
    if (fb_map(b) < 0)
        return;
    int W = b->fi.w, H = b->fi.h;
    size_t len = b->fb_len;
    if (!b->under) {
        b->under = malloc(len);
        b->shown = malloc(len);
        b->frame = malloc((size_t)W * (size_t)H * 4);
        if (!b->under || !b->shown || !b->frame) {
            free(b->under), free(b->shown), free(b->frame);
            b->under = b->shown = NULL;
            b->frame = NULL;
            return;
        }
    }
    if (!b->under_ok) {
        /* what the firmware and GRUB left (the checkerboard), restored on hide */
        memcpy(b->under, b->fb + b->fb_off, len);
        memcpy(b->shown, b->under, len);
        b->under_ok = b->shown_ok = true;
    }
    uint8_t *tgt = malloc(len);
    if (!tgt)
        return;
    memcpy(tgt, b->under, len);
    int s = rb_effective_scale(W, H, rb_boot_pixel_scale(NULL));
    if (want == 2) {
        rb_diag_render(b->frame, W, H, W, s, &b->doc);
        for (int y = 0; y < H; y++)
            bd_fb_convert_row(b->frame + (size_t)y * W, tgt + (size_t)y * b->fi.pitch, W, &b->fi);
    } else if (want == 1) {
        rb_rect r = rb_diag_hint_rect(W, H, s);
        for (int y = r.y; y < r.y + r.h; y++)
            bd_fb_decode_row(b->under + (size_t)y * b->fi.pitch, b->frame + (size_t)y * W, W, &b->fi);
        rb_diag_render_hint(b->frame, W, H, W, s);
        for (int y = r.y; y < r.y + r.h; y++)
            bd_fb_convert_row(b->frame + (size_t)y * W, tgt + (size_t)y * b->fi.pitch, W, &b->fi);
    }
    fb_write(b, tgt);
    free(tgt);
    b->fb_touched = want != 0;
    b->fb_view_gen = b->doc_gen;
}

/* ------------------------------------------------------------------------ */
/* report                                                                   */

static int cmp_str(const void *a, const void *b) { return strcmp(a, b); }

static const char *const mark_txt[] = {"ok  ", "FAIL", "... ", "-   "}; /* plain ASCII: any text editor */

static void report_build(struct bd *b, struct buf *o, const char *reason)
{
    uint64_t now = now_ms();
    char tm[16], wall[64];
    time_t t = time(NULL);
    struct tm g;
    gmtime_r(&t, &g);
    strftime(wall, sizeof wall, "%Y-%m-%d %H:%M:%S UTC", &g);
    bd_fmt_secs(now, tm, sizeof tm);
    bprintf(o, "RetroBoot Startup Details\n=========================\n\n");
    bprintf(o, "Saved:      %s, %s after power-on (%s)\n", wall, tm, reason);
    bprintf(o, "Boot id:    %s\n", b->boot_id);
    bprintf(o, "RetroBoot:  %s (%s)\n", b->build, b->cfg.installer ? "installer" : "installed system");
    char v[512];
    if (read_trim("/proc/version", v, sizeof v))
        bprintf(o, "Kernel:     %s\n", v);
    char cmd[4096];
    if (read_trim("/proc/cmdline", cmd, sizeof cmd))
        bprintf(o, "Command line: %s\n", cmd);
    bprintf(o, "Computer:   %s\n", b->dmi[0]);
    if (b->dt) {
        if (b->dmi[1][0])
            bprintf(o, "Firmware:   %s\n", b->dmi[1]);
        if (b->dmi[2][0])
            bprintf(o, "Board:      %s\n", b->dmi[2]);
    } else {
        bprintf(o, "BIOS:       %s\n", b->dmi[1]);
        if (b->dmi[2][0])
            bprintf(o, "Board:      %s\n", b->dmi[2]);
    }
    bprintf(o, "Processor:  %s\n", b->cpu);
    bprintf(o, "Memory:     %s\n\n", b->mem);

    bprintf(o, "Startup\n-------\n");
    for (int s = 0; s < BD_NSTAGES; s++) {
        if (b->st.skipped[s])
            continue;
        char lb[128];
        const char *label = stage_label(b, s, lb, sizeof lb);
        int st = b->st.done[s] ? 0 : b->st.failed[s] ? 1 : 2;
        char when[16] = "";
        if (b->st.done[s])
            bd_fmt_secs(b->st.ms[s], when, sizeof when);
        bprintf(o, "  %s %-48s %10s  [%s]\n", mark_txt[st], label, when, bd_stage_id(s));
    }
    bprintf(o, "  Progress published: %s\n", b->pub_line[0] ? b->pub_line : "(none)\n");

    bprintf(o, "Problems (most important first)\n-------------------------------\n");
    char fp[RB_DIAG_LINE];
    if (flips_problem(b, fp, sizeof fp))
        bprintf(o, "  %s\n", fp);
    if (b->pr.alive && b->pr.age_s >= 10)
        bprintf(o, "  The RetroBoot screen has not responded for %ld s\n", b->pr.age_s);
    if (b->st.failed[BD_DISPLAY])
        bprintf(o, "  No display driver started: %s\n", b->st.detail[BD_DISPLAY]);
    if (b->failed_units[0])
        bprintf(o, "  Failed services: %s\n", b->failed_units);
    usb_storage_eval(b, now);
    for (int i = 0; i < b->nusbstor; i++)
        bprintf(o, "  USB storage %s has no driver (usb-storage and uas not loaded)\n", b->usbstor[i]);
    char rpw[160];
    bool ruv = false;
    if (b->pi && pi_power(rpw, sizeof rpw, &ruv) && ruv)
        bprintf(o, "  Under-voltage: the power supply is too weak for this Raspberry Pi (%s)\n", rpw);
    const struct problem *pp[MAXPROB];
    int np = sorted_problems(b, pp, MAXPROB, 1);
    for (int i = 0; i < np; i++) {
        bd_fmt_secs(pp[i]->ms, tm, sizeof tm);
        bprintf(o, "  [%3d] %9s  %s%s\n", pp[i]->score, tm, pp[i]->text, pp[i]->count > 1 ? " (repeated)" : "");
    }
    if (!np && !b->st.failed[BD_DISPLAY])
        bprintf(o, "  none found\n");

    bprintf(o, "\nDisplay adapters\n----------------\n");
    pci_scan(b);
    for (int i = 0; i < b->npci; i++) {
        struct pcidev *p = &b->pci[i];
        pci_expect(p);
        const char *ven = bd_pci_vendor(p->vendor);
        bprintf(o, "  %s class %06x %04x:%04x (subsystem %04x:%04x) %s: driver %s%s%s%s%s%s\n", p->addr, p->cls,
                p->vendor, p->device, p->svendor, p->sdevice, ven ? ven : "", p->driver[0] ? p->driver : "none",
                p->expect[0] ? ", module for it: " : "", p->expect, blocked(p->expect) ? " (blocked on the command line)" : "",
                p->boot_vga ? ", boot VGA" : "", p->has_fb ? ", holds the firmware framebuffer" : "");
        if (p->probe_err[0])
            bprintf(o, "      %s\n", p->probe_err);
    }
    char cards[4][16], drivers[4][32];
    int ncard = drm_cards(cards, drivers, 4);
    for (int i = 0; i < ncard; i++)
        bprintf(o, "  DRM %s: driver %s\n", cards[i], drivers[i]);
    char conn[8][96];
    int nconn = drm_connected(conn, 8);
    for (int i = 0; i < nconn; i++)
        bprintf(o, "    %s\n", conn[i]);
    if (!ncard)
        bprintf(o, "  No DRM (KMS) display device.\n");
    static char rpanels[4][RB_DIAG_LINE];
    int nrp = panel_lines(b, rpanels, 4);
    for (int i = 0; i < nrp; i++)
        bprintf(o, "  %s\n", rpanels[i]);
    char ropts[200];
    bprintf(o, "  Display driver options: %s\n", driver_options(ropts, sizeof ropts) ? ropts : "(no amdgpu or i915 loaded)");
    for (int i = 0; i < 2; i++) {
        const struct flipinfo *f = &b->flips[i];
        if (f->present)
            bprintf(o, "  Display updates seen by the %s: %u page flips pending over 1 s (first at %llu ms, last at "
                       "%llu ms) on %s (%s); %u modesets over 1 s (longest %llu ms)\n",
                    i ? "splash" : "presenter", f->stalls, (unsigned long long)f->first_ms,
                    (unsigned long long)f->last_ms, f->conn, f->drv, f->slow, (unsigned long long)f->slow_max);
        else
            bprintf(o, "  Display updates seen by the %s: no stalls reported\n", i ? "splash" : "presenter");
    }
    if (b->dt) {
        char dd[200];
        dt_display(b, dd, sizeof dd);
        bprintf(o, "  Raspberry Pi display: %s\n", dd);
        static char routs[8][RB_DIAG_LINE];
        int no = outputs_edid(routs, 8);
        for (int i = 0; i < no; i++)
            bprintf(o, "  %s\n", routs[i]);
        if (b->pi && pi_power(rpw, sizeof rpw, &ruv))
            bprintf(o, "  Power and temperature: %s\n", rpw);
        char cmdtxt[64];
        if (read_trim("/sys/module/vc4/initstate", cmdtxt, sizeof cmdtxt))
            bprintf(o, "  vc4 module: %s\n", cmdtxt);
    }
    if (b->fi_ok && b->fbdev) {
        bprintf(o, "  Firmware framebuffer: %dx%d, %d bpp, pitch %d (/dev/fb0); %s\n", b->fi.w, b->fi.h,
                b->fi.depth, b->fi.pitch, b->fb_state);
    } else if (b->fi_ok) {
        char plat[32] = "";
        bool pl = platform_fb_exists(plat, sizeof plat);
        bprintf(o, "  Firmware framebuffer: %dx%d, %d bpp, pitch %d, at 0x%llx (%s); platform device %s; %s\n",
                b->fi.w, b->fi.h, b->fi.depth, b->fi.pitch, (unsigned long long)b->fi.base,
                b->fi.type == 0x70 ? "EFI GOP" : "VESA", pl ? plat : "gone", b->fb_state);
    } else {
        bprintf(o, "  Firmware framebuffer: not described\n");
    }
    if (b->pr.present) {
        char disp[128];
        if (b->pr.display)
            sfmt(disp, sizeof disp, "%s on %s at %s", b->pr.driver, b->pr.device, b->pr.mode);
        else
            sfmt(disp, sizeof disp, "none");
        bprintf(o, "  RetroBoot screen (presenter pid %d, %s): display %s, scene %s, view %s, input %s, service %s, "
                   "last heard of %ld s ago\n",
                b->pr.pid, b->pr.alive ? "running" : "gone", disp, b->pr.scene, b->pr.view,
                b->pr.input ? "yes" : "no", b->pr.hostd ? "connected" : "not connected", b->pr.age_s);
    }
    else
        bprintf(o, "  RetroBoot screen: not started\n");

    bprintf(o, "\nKeyboards\n---------\n");
    for (int i = 0; i < MAXIN; i++)
        if (b->in[i].fd >= 0)
            bprintf(o, "  %s (%s)\n", b->in[i].name, b->in[i].path);
    char devs[16384];
    if (rb_diag_read_file("/proc/bus/input/devices", devs, sizeof devs) > 0) {
        bprintf(o, "  All input devices:\n");
        char *save = NULL;
        for (char *l = strtok_r(devs, "\n", &save); l; l = strtok_r(NULL, "\n", &save))
            if (!strncmp(l, "N: ", 3) || !strncmp(l, "H: ", 3))
                bprintf(o, "    %s\n", l);
    }

    bprintf(o, "\nDisks\n-----\n");
    if (b->medium[0])
        bprintf(o, "  Installer disk: %s\n", b->medium);
    static char blk[64][32];
    int nblk = 0;
    DIR *dir = opendir("/sys/class/block");
    if (dir) {
        struct dirent *e;
        while ((e = readdir(dir)) && nblk < 64)
            if (e->d_name[0] != '.' && strncmp(e->d_name, "loop", 4) && strncmp(e->d_name, "ram", 3) &&
                strncmp(e->d_name, "zram", 4))
                sfmt(blk[nblk++], sizeof blk[0], "%s", e->d_name);
        closedir(dir);
    }
    qsort(blk, (size_t)nblk, sizeof blk[0], cmp_str);
    for (int bi = 0; bi < nblk; bi++) {
        const char *name = blk[bi];
        {
            char path[PATH_MAX], sz[32] = "", rm[8] = "", model[96] = "", vendor[64] = "", part[8] = "";
            sfmt(path, sizeof path, "/sys/class/block/%s/size", name);
            read_trim(path, sz, sizeof sz);
            sfmt(path, sizeof path, "/sys/class/block/%s/removable", name);
            read_trim(path, rm, sizeof rm);
            sfmt(path, sizeof path, "/sys/class/block/%s/partition", name);
            bool is_part = read_trim(path, part, sizeof part);
            sfmt(path, sizeof path, "/sys/class/block/%s/device/model", name);
            read_trim(path, model, sizeof model);
            sfmt(path, sizeof path, "/sys/class/block/%s/device/vendor", name);
            read_trim(path, vendor, sizeof vendor);
            char label[16] = "";
            if (is_part) {
                char dev[64];
                sfmt(dev, sizeof dev, "/dev/%s", name);
                int fd = open(dev, O_RDONLY | O_CLOEXEC | O_NONBLOCK);
                if (fd >= 0) {
                    uint8_t bs[512];
                    if (pread(fd, bs, sizeof bs, 0) == (ssize_t)sizeof bs)
                        bd_fat_label(bs, sizeof bs, label, sizeof label);
                    close(fd);
                }
            }
            double bytes = strtoull(sz, NULL, 10) * 512.0;
            char size[32];
            if (bytes >= 1e9)
                sfmt(size, sizeof size, "%.2f GB", bytes / 1e9);
            else
                sfmt(size, sizeof size, "%.1f MB", bytes / 1e6);
            bprintf(o, "  %s%-10s %11s%s%s%s%s%s%s%s\n", is_part ? "  " : "", name, size,
                    rm[0] == '1' ? " removable" : "", vendor[0] ? " " : "", vendor, model[0] ? " " : "", model,
                    label[0] ? " FAT label " : "", label);
        }
    }

    bprintf(o, "\nKernel log (latest %d messages)\n------------------------------\n", b->ring_n);
    for (int i = b->ring_n - 1; i >= 0; i--) {
        const struct bd_kmsg *m = &b->ring[(b->ring_head - 1 - i + RING) % RING];
        bprintf(o, "<%d>[%5llu.%06llu] %s\n", m->prio, (unsigned long long)(m->ts_us / 1000000),
                (unsigned long long)(m->ts_us % 1000000), m->text);
    }
}

/* ------------------------------------------------------------------------ */
/* commands (root instance only)                                            */

/* Run argv with a timeout; stdout+stderr into out (cap); returns the exit status or -1. */
static int run_capture(char *const argv[], char *out, size_t cap, int timeout_ms)
{
    int pfd[2];
    if (pipe2(pfd, O_CLOEXEC) < 0)
        return -1;
    pid_t pid = fork();
    if (pid < 0) {
        close(pfd[0]);
        close(pfd[1]);
        return -1;
    }
    if (pid == 0) {
        dup2(pfd[1], 1);
        dup2(pfd[1], 2);
        int nul = open("/dev/null", O_RDONLY);
        if (nul >= 0)
            dup2(nul, 0);
        execv(argv[0], argv);
        _exit(127);
    }
    close(pfd[1]);
    size_t n = 0;
    uint64_t end = now_ms() + (uint64_t)timeout_ms;
    fcntl(pfd[0], F_SETFL, O_NONBLOCK);
    for (;;) {
        uint64_t now = now_ms();
        if (now >= end) {
            kill(pid, SIGKILL);
            break;
        }
        struct timespec ts = {0, 20 * 1000 * 1000};
        ssize_t k = read(pfd[0], out + n, cap - 1 - n);
        if (k > 0) {
            n += (size_t)k;
            if (n >= cap - 1)
                break;
            continue;
        }
        if (k == 0)
            break;
        nanosleep(&ts, NULL);
    }
    close(pfd[0]);
    out[n] = 0;
    int status = 0;
    if (waitpid(pid, &status, 0) < 0)
        return -1;
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

static bool have_systemd(void)
{
    char comm[32];
    return read_trim("/proc/1/comm", comm, sizeof comm) && !strcmp(comm, "systemd") &&
           exists("/usr/bin/systemctl");
}

static void failed_units_update(struct bd *b)
{
    if (b->initramfs || !have_systemd())
        return;
    char out[4096];
    char *argv[] = {"/usr/bin/systemctl", "--failed", "--no-legend", "--plain", "--no-pager", NULL};
    if (run_capture(argv, out, sizeof out, 2000) < 0)
        return;
    char units[512] = "";
    size_t n = 0;
    char *save = NULL;
    for (char *l = strtok_r(out, "\n", &save); l; l = strtok_r(NULL, "\n", &save)) {
        char u[128];
        if (sscanf(l, "%127s", u) == 1 && strchr(u, '.') && n < sizeof units) {
            int k = snprintf(units + n, sizeof units - n, "%s%s", n ? ", " : "", u);
            n = k > 0 && n + (size_t)k < sizeof units ? n + (size_t)k : sizeof units - 1;
        }
    }
    if (strcmp(units, b->failed_units)) {
        sfmt(b->failed_units, sizeof b->failed_units, "%s", units);
        b->dirty = true;
    }
    b->failed_ms = now_ms();
}

/* ------------------------------------------------------------------------ */
/* saving the report (in a child process: a slow or broken USB stick never
 * holds up the screen or Escape)                                            */

static bool fat_label_of(const char *dev, char *label, size_t cap)
{
    int fd = open(dev, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return false;
    uint8_t bs[512];
    bool ok = pread(fd, bs, sizeof bs, 0) == (ssize_t)sizeof bs && bd_fat_label(bs, sizeof bs, label, cap) == 0;
    close(fd);
    return ok;
}

/* The RBINSTEFI partition: on the installer medium's disk, else the only one anywhere. */
static bool find_rbinstefi(const char *medium, char *dev, size_t cap)
{
    char disk[64] = "";
    if (medium && medium[0]) {
        const char *name = strrchr(medium, '/');
        name = name ? name + 1 : medium;
        char path[PATH_MAX], real[PATH_MAX];
        sfmt(path, sizeof path, "/sys/class/block/%s", name);
        if (realpath(path, real)) {
            char pf[PATH_MAX], tmp[8];
            sfmt(pf, sizeof pf, "%s/partition", real);
            if (read_trim(pf, tmp, sizeof tmp)) { /* a partition: its disk */
                char *sl = strrchr(real, '/');
                if (sl)
                    *sl = 0;
            }
            const char *dn = strrchr(real, '/');
            sfmt(disk, sizeof disk, "%s", dn ? dn + 1 : real);
        }
    }
    int found = 0;
    char cand[64] = "";
    DIR *dir = opendir("/sys/class/block");
    if (!dir)
        return false;
    struct dirent *e;
    while ((e = readdir(dir))) {
        char path[PATH_MAX], tmp[8];
        sfmt(path, sizeof path, "/sys/class/block/%s/partition", e->d_name);
        if (!read_trim(path, tmp, sizeof tmp))
            continue;
        if (disk[0] && strncmp(e->d_name, disk, strlen(disk)))
            continue;
        char d[64], label[16];
        sfmt(d, sizeof d, "/dev/%s", e->d_name);
        if (fat_label_of(d, label, sizeof label) && !strcmp(label, "RBINSTEFI")) {
            sfmt(cand, sizeof cand, "%s", d);
            found++;
        }
    }
    closedir(dir);
    if (found == 1 || (found && disk[0])) {
        sfmt(dev, cap, "%s", cand);
        return true;
    }
    return false;
}

static int write_file_sync(const char *dir, const char *name, const char *data, size_t len)
{
    char tmp[PATH_MAX], fin[PATH_MAX];
    sfmt(tmp, sizeof tmp, "%s/.%s.tmp", dir, name);
    sfmt(fin, sizeof fin, "%s/%s", dir, name);
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd < 0)
        return -1;
    size_t off = 0;
    while (off < len) {
        ssize_t k = write(fd, data + off, len - off);
        if (k < 0 && errno == EINTR)
            continue;
        if (k <= 0) {
            close(fd);
            unlink(tmp);
            return -1;
        }
        off += (size_t)k;
    }
    if (fsync(fd) < 0 || close(fd) < 0 || rename(tmp, fin) < 0) {
        unlink(tmp);
        return -1;
    }
    int dfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (dfd >= 0) {
        fsync(dfd);
        close(dfd);
    }
    return 0;
}

static void prune_dir(const char *dir, const char *own)
{
    static char names[64][64];
    int n = 0;
    DIR *d = opendir(dir);
    if (!d)
        return;
    struct dirent *e;
    while ((e = readdir(d)) && n < 64)
        if (bd_report_name_ok(e->d_name))
            sfmt(names[n++], 64, "%s", e->d_name);
    closedir(d);
    int del[64];
    int k = bd_prune(names, n, BD_REPORT_KEEP, own, del);
    for (int i = 0; i < k; i++) {
        char p[PATH_MAX];
        sfmt(p, sizeof p, "%s/%s", dir, names[del[i]]);
        unlink(p);
    }
}

static void append_cmd(struct buf *o, const char *title, char *const argv[], int timeout_ms)
{
    if (!exists(argv[0]))
        return;
    char *out = malloc(64 * 1024);
    if (!out)
        return;
    run_capture(argv, out, 64 * 1024, timeout_ms);
    bprintf(o, "\n%s\n", title);
    for (size_t i = strlen(title); i > 0; i--)
        bprintf(o, "-");
    bprintf(o, "\n%s\n", out[0] ? out : "(nothing)");
    free(out);
}

static int mkdir_p(const char *base, const char *rel)
{
    char p[PATH_MAX];
    sfmt(p, sizeof p, "%s/%s", base, rel);
    for (char *s = p + strlen(base) + 1; *s; s++)
        if (*s == '/') {
            *s = 0;
            mkdir(p, 0755);
            *s = '/';
        }
    return mkdir(p, 0755) == 0 || errno == EEXIST ? 0 : -1;
}

/* Is `dev` (a partition) blocked: its whole disk or itself mounted? Compared
 * by device number (mountinfo's third field): the installer mounted its medium
 * through a /dev/disk/by-label link, so the names differ. */
static bool dev_busy(const char *dev)
{
    const char *name = strrchr(dev, '/');
    name = name ? name + 1 : dev;
    char path[PATH_MAX], real[PATH_MAX], part_mm[32] = "", disk_mm[32] = "";
    sfmt(path, sizeof path, "/sys/class/block/%s/dev", name);
    read_trim(path, part_mm, sizeof part_mm);
    sfmt(path, sizeof path, "/sys/class/block/%s", name);
    if (realpath(path, real)) {
        char *sl = strrchr(real, '/');
        if (sl) {
            *sl = 0;
            sfmt(path, sizeof path, "%s/dev", real);
            read_trim(path, disk_mm, sizeof disk_mm);
        }
    }
    static char mi[64 * 1024];
    if (rb_diag_read_file("/proc/self/mountinfo", mi, sizeof mi) <= 0)
        return false;
    char *save = NULL;
    for (char *l = strtok_r(mi, "\n", &save); l; l = strtok_r(NULL, "\n", &save)) {
        char mm[32];
        if (sscanf(l, "%*s %*s %31s", mm) != 1)
            continue;
        if ((part_mm[0] && !strcmp(mm, part_mm)) || (disk_mm[0] && !strcmp(mm, disk_mm)))
            return true;
    }
    return false;
}

/* The child: returns "ok <where>" / "busy <dev>" / "err <why>" in res. */
static void save_child(struct bd *b, struct buf *rep, char *res, size_t rcap)
{
    if (!b->initramfs && have_systemd()) {
        char *sys[] = {"/usr/bin/systemctl", "--failed", "--no-pager", "--plain", NULL};
        append_cmd(rep, "Failed services", sys, 3000);
        char *act[] = {"/usr/bin/systemctl", "status", "--no-pager", "--lines=0", "retroboot-presenter.service",
                       "retroboot-hostd.service", NULL};
        append_cmd(rep, "RetroBoot services", act, 3000);
        char *jr[] = {"/usr/bin/journalctl", "-b", "-p", "warning", "-n", "120", "--no-pager", "-o",
                      "short-monotonic", NULL};
        append_cmd(rep, "Journal: warnings and errors (latest 120)", jr, 5000);
    }
    /* installed system with the data partition: its logs folder */
    if (!b->cfg.installer) {
        char mi[16384];
        if (rb_diag_read_file("/proc/self/mountinfo", mi, sizeof mi) > 0 && strstr(mi, " /var/lib/retroboot ")) {
            if (mkdir_p("/var/lib/retroboot", "logs/startup-details") == 0 &&
                write_file_sync("/var/lib/retroboot/logs/startup-details", b->save_name, rep->p, rep->n) == 0) {
                prune_dir("/var/lib/retroboot/logs/startup-details", b->save_name);
                sfmt(res, rcap, "ok in logs/startup-details/%s on the data partition", b->save_name);
                return;
            }
        }
    }
    char dev[64] = "";
    const char *what;
    if (b->cfg.installer) {
        char medium[128] = "";
        read_trim("/run/retroboot/live/medium-device", medium, sizeof medium);
        if (!find_rbinstefi(medium[0] ? medium : b->medium, dev, sizeof dev)) {
            sfmt(res, rcap, "err no writable USB stick partition (RBINSTEFI) found%s",
                     medium[0] && !strncmp(medium, "/dev/sr", 7) ? ": started from a DVD" : "");
            return;
        }
        what = b->dt ? "on the installer's SD card or USB stick" : "on the USB stick";
    } else {
        if (exists("/dev/disk/by-partlabel/RBEFI") && realpath("/dev/disk/by-partlabel/RBEFI", dev)) {
            what = "on the EFI partition";
        } else if (find_rbinstefi(NULL, dev, sizeof dev)) {
            /* a Raspberry Pi card with RetroBoot beside the installer (ARCHITECTURE 8.2) */
            what = "on the card's startup partition (RBINSTEFI)";
        } else {
            sfmt(res, rcap, "err no data or EFI partition to save to");
            return;
        }
    }
    /* A file system mounted from the whole disk (the installer reading the
     * ISO 9660 tree until rb-live-toram released it) claims the disk, so its
     * partitions cannot be mounted (EBUSY, and the kernel logs "Can't open
     * blockdev"); nor can a partition mounted read-only by someone else
     * (hostd's network.conf). Try again shortly instead. */
    if (dev_busy(dev)) {
        sfmt(res, rcap, "busy %s", dev);
        return;
    }
    char mnt[96];
    sfmt(mnt, sizeof mnt, "%s/mnt-%d", RB_DIAG_DIR, (int)getpid());
    mkdir(mnt, 0700);
    int rc = -1, err = 0;
    for (int attempt = 0; attempt < 2; attempt++) {
        rc = mount(dev, mnt, "vfat", MS_NOSUID | MS_NODEV | MS_NOEXEC | MS_NOATIME,
                   "codepage=437,iocharset=ascii,shortname=mixed");
        err = errno;
        if (rc == 0 || err != ENODEV)
            break;
        char *mp[] = {"/sbin/modprobe", "-q", "vfat", NULL}; /* vfat not loaded yet */
        char tmp[256];
        run_capture(mp, tmp, sizeof tmp, 5000);
    }
    if (rc != 0) {
        rmdir(mnt);
        if (err == EBUSY)
            sfmt(res, rcap, "busy %s", dev);
        else
            sfmt(res, rcap, "err cannot open %s (%s)", dev, strerror(err));
        return;
    }
    char dir[PATH_MAX];
    sfmt(dir, sizeof dir, "%s/%s", mnt, BD_REPORT_DIR);
    if (mkdir_p(mnt, BD_REPORT_DIR) < 0 || write_file_sync(dir, b->save_name, rep->p, rep->n) < 0) {
        sfmt(res, rcap, "err cannot write to %s (%s)", dev, strerror(errno));
    } else {
        prune_dir(dir, b->save_name);
        sfmt(res, rcap, "ok %s as %s/%s", what, BD_REPORT_DIR, b->save_name);
    }
    sync();
    for (int i = 0; i < 10 && umount(mnt) < 0 && errno == EBUSY; i++)
        usleep(100 * 1000);
    if (umount2(mnt, MNT_DETACH) < 0 && errno != EINVAL)
        rb_log("umount %s: %s", mnt, strerror(errno));
    rmdir(mnt);
}

static void save_done(void *data, int fd, uint32_t events);

static void save_start(struct bd *b, const char *reason)
{
    if (b->save_pid > 0) {
        b->save_again = true;
        sfmt(b->save_reason, sizeof b->save_reason, "%s", reason);
        return;
    }
    /* A Raspberry Pi 4 has no real-time clock: a name taken in the initramfs says 1970.
     * Once the clock is set (systemd's clock-epoch, NTP) the report takes a real name; the
     * early file is the oldest and pruned first. */
    if (!b->save_name[0] || (!strncmp(b->save_name, "19", 2) && time(NULL) > 978307200))
        bd_report_name(time(NULL), b->boot_id, b->save_name, sizeof b->save_name);
    struct buf rep = {malloc(REPORT_CAP), 0, REPORT_CAP};
    if (!rep.p)
        return;
    rep.p[0] = 0;
    report_build(b, &rep, reason);
    int pfd[2];
    if (pipe2(pfd, O_CLOEXEC) < 0) {
        free(rep.p);
        return;
    }
    fflush(NULL);
    pid_t pid = fork();
    if (pid < 0) {
        close(pfd[0]);
        close(pfd[1]);
        free(rep.p);
        return;
    }
    if (pid == 0) {
        close(pfd[0]);
        char res[256];
        save_child(b, &rep, res, sizeof res);
        ssize_t k = write(pfd[1], res, strlen(res));
        (void)k;
        _exit(0);
    }
    free(rep.p);
    close(pfd[1]);
    fcntl(pfd[0], F_SETFL, O_NONBLOCK);
    b->save_pid = pid;
    b->save_fd = pfd[0];
    sfmt(b->save_last_reason, sizeof b->save_last_reason, "%s", reason);
    b->save_started = b->last_save_start = now_ms();
    b->save_watch = rb_loop_watch(b->loop, pfd[0], EPOLLIN, save_done, b);
    rb_log("saving the report (%s) as %s", reason, b->save_name);
    b->dirty = true;
}

static void save_finish(struct bd *b, const char *res)
{
    bool killed = !strcmp(res, "err the USB stick did not answer");
    if (b->save_watch)
        rb_watch_remove(b->save_watch);
    b->save_watch = NULL;
    if (b->save_fd >= 0)
        close(b->save_fd);
    b->save_fd = -1;
    if (b->save_pid > 0)
        waitpid(b->save_pid, NULL, killed ? WNOHANG : 0);
    b->save_pid = 0;
    b->save_retry_at = 0;
    if (!strncmp(res, "busy ", 5)) {
        /* the installer still reads the stick: try again in a moment */
        b->save_retry_at = now_ms() + 3000;
        b->save_busy = true;
        snprintf(b->save_reason, sizeof b->save_reason, "%s", b->save_last_reason);
    } else if (!strncmp(res, "ok ", 3)) {
        b->save_busy = false;
        char tm[16];
        bd_fmt_secs(now_ms(), tm, sizeof tm);
        sfmt(b->saved_where, sizeof b->saved_where, "%s (at %s)", res + 3, tm);
        b->save_err[0] = 0;
        b->last_save_done_start = b->save_started;
    } else {
        b->save_busy = false;
        sfmt(b->save_err, sizeof b->save_err, "%s", !strncmp(res, "err ", 4) ? res + 4 : res);
    }
    rb_log("report: %s", res);
    b->dirty = true;
    view_update(b, true);
    if (b->save_again) {
        b->save_again = false;
        save_start(b, b->save_reason);
    }
}

static void save_done(void *data, int fd, uint32_t events)
{
    (void)events;
    struct bd *b = data;
    char res[256];
    ssize_t n = read(fd, res, sizeof res - 1);
    if (n < 0 && errno == EAGAIN)
        return;
    res[n > 0 ? n : 0] = 0;
    save_finish(b, n > 0 ? res : "err the save stopped unexpectedly");
}

/* ------------------------------------------------------------------------ */
/* state across switch_root                                                 */

static void state_save(struct bd *b)
{
    struct buf o = {malloc(64 * 1024), 0, 64 * 1024};
    if (!o.p)
        return;
    o.p[0] = 0;
    bprintf(&o, "v 1\n");
    for (int s = 0; s < BD_NSTAGES; s++)
        bprintf(&o, "stage %d %d %d %d %llu %s\n", s, b->st.done[s], b->st.failed[s], b->st.skipped[s],
                (unsigned long long)b->st.ms[s], b->st.detail[s]);
    for (int i = 0; i < b->nprob; i++)
        bprintf(&o, "prob %d %llu %d %s\n", b->prob[i].score, (unsigned long long)b->prob[i].ms, b->prob[i].count,
                b->prob[i].text);
    bprintf(&o, "pub %d\n", b->st.published);
    bprintf(&o, "publine %s", b->pub_line[0] ? b->pub_line : "\n");
    bprintf(&o, "name %s\nsaved %s\nerr %s\n", b->save_name, b->saved_where, b->save_err);
    bprintf(&o, "opened %d\nauto %d %d\nhide %llu\nlastsave %llu\n", b->opened_ever, b->auto1, b->auto2,
            (unsigned long long)b->hide_ms, (unsigned long long)b->last_save_done_start);
    bprintf(&o, "medium %s\n", b->medium);
    bprintf(&o, "kseq %llu\n", (unsigned long long)b->kmsg_seq);
    bprintf(&o, "fb %d %d\n", b->under_ok, b->fb_touched);
    rb_diag_write_atomic(RB_DIAG_STATE, o.p, o.n);
    if (b->under_ok && b->under)
        rb_diag_write_atomic(RB_DIAG_DIR "/fb-under", (const char *)b->under, b->fb_len);
    free(o.p);
}

static void state_load(struct bd *b)
{
    char *buf = malloc(64 * 1024);
    if (!buf)
        return;
    if (rb_diag_read_file(RB_DIAG_STATE, buf, 64 * 1024) <= 0) {
        free(buf);
        return;
    }
    char *save = NULL;
    for (char *l = strtok_r(buf, "\n", &save); l; l = strtok_r(NULL, "\n", &save)) {
        int s, dn, fl, sk, sc, cnt, n = 0;
        unsigned long long ms, a, c;
        if (sscanf(l, "stage %d %d %d %d %llu %n", &s, &dn, &fl, &sk, &ms, &n) == 5 && s >= 0 && s < BD_NSTAGES) {
            b->st.done[s] = dn;
            b->st.failed[s] = fl;
            b->st.skipped[s] = sk;
            b->st.ms[s] = ms;
            sfmt(b->st.detail[s], sizeof b->st.detail[s], "%s", l + n);
        } else if (sscanf(l, "prob %d %llu %d %n", &sc, &ms, &cnt, &n) == 3 && n) {
            add_problem(b, sc, ms, l + n);
        } else if (sscanf(l, "pub %d", &sc) == 1) {
            b->st.published = sc;
        } else if (!strncmp(l, "publine ", 8)) {
            sfmt(b->pub_line, sizeof b->pub_line, "%s\n", l + 8);
        } else if (!strncmp(l, "name ", 5)) {
            sfmt(b->save_name, sizeof b->save_name, "%s", l + 5);
        } else if (!strncmp(l, "saved ", 6)) {
            sfmt(b->saved_where, sizeof b->saved_where, "%s", l + 6);
        } else if (!strncmp(l, "err ", 4)) {
            sfmt(b->save_err, sizeof b->save_err, "%s", l + 4);
        } else if (sscanf(l, "opened %d", &sc) == 1) {
            b->opened_ever = sc;
        } else if (sscanf(l, "auto %d %d", &dn, &fl) == 2) {
            b->auto1 = dn;
            b->auto2 = fl;
        } else if (sscanf(l, "hide %llu", &a) == 1) {
            b->hide_ms = a;
        } else if (sscanf(l, "lastsave %llu", &c) == 1) {
            b->last_save_done_start = c;
        } else if (sscanf(l, "kseq %llu", &a) == 1) {
            b->seq_known = a;
        } else if (!strncmp(l, "medium ", 7)) {
            sfmt(b->medium, sizeof b->medium, "%s", l + 7);
        } else if (sscanf(l, "fb %d %d", &dn, &fl) == 2) {
            b->under_ok = dn;
            b->fb_touched = fl;
        }
    }
    free(buf);
    if (b->under_ok && b->fi_ok) {
        size_t len = (size_t)b->fi.pitch * (size_t)b->fi.h;
        b->under = malloc(len);
        b->shown = malloc(len);
        b->frame = malloc((size_t)b->fi.w * (size_t)b->fi.h * 4);
        if (b->under && b->shown && b->frame &&
            rb_diag_read_file(RB_DIAG_DIR "/fb-under", (char *)b->under, len + 1) == (ssize_t)len) {
            b->fb_len = len;
            b->shown_ok = false;
        } else {
            b->under_ok = false;
        }
    } else {
        b->under_ok = false;
    }
    b->st.published = 0; /* re-derived (monotonic within the progress file anyway) */
    char line[160];
    if (rb_diag_read_file(RB_DIAG_PROGRESS, line, sizeof line) > 0)
        b->st.published = atoi(line);
}

/* ------------------------------------------------------------------------ */
/* the view's visibility                                                    */

static void visible_sync(struct bd *b)
{
    bool vis = exists(RB_DIAG_VISIBLE), hint = exists(RB_DIAG_HINT);
    if (hint != b->hint) {
        b->hint = hint;
        if (!b->visible)
            fb_draw(b);
    }
    if (vis == b->visible)
        return;
    b->visible = vis;
    uint64_t now = now_ms();
    rb_log("view %s", vis ? "shown" : "hidden");
    if (vis) {
        b->opened_ever = true;
        b->visible_since = now;
        failed_units_update(b);
        b->dirty = true;
        view_update(b, true);
        save_start(b, "the window was opened");
    } else {
        b->hide_ms = now;
        fb_draw(b);
        save_start(b, "the window was closed");
    }
}

static void set_visible(struct bd *b, bool on)
{
    if (on) {
        b->dirty = true;
        view_update(b, true); /* fresh content before anyone draws it */
    }
    rb_diag_set_visible(on);
    visible_sync(b);
}

/* ------------------------------------------------------------------------ */
/* keyboards                                                                */

static void input_close(struct bd *b, int i)
{
    if (b->in[i].fd < 0)
        return;
    rb_watch_remove(b->in[i].w);
    close(b->in[i].fd);
    b->in[i].fd = -1;
    b->dirty = true;
}

static void input_ready(void *data, int fd, uint32_t events)
{
    struct bd *b = data;
    int idx = -1;
    for (int i = 0; i < MAXIN; i++)
        if (b->in[i].fd == fd)
            idx = i;
    if (idx < 0)
        return;
    struct input_event ev[32];
    for (;;) {
        ssize_t n = read(fd, ev, sizeof ev);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0) {
            if (n == 0 || (errno != EAGAIN) || (events & (EPOLLHUP | EPOLLERR)))
                if (!(n < 0 && errno == EAGAIN))
                    input_close(b, idx);
            return;
        }
        for (size_t k = 0; k < (size_t)n / sizeof ev[0]; k++) {
            if (ev[k].type != EV_KEY || ev[k].code != KEY_ESC || ev[k].value != 1)
                continue;
            if (presenter_owns_input(b)) {
                rb_log("Escape: the presenter has input; it decides");
                continue;
            }
            rb_log("Escape on %s: %s the view", b->in[idx].name, b->visible ? "hiding" : "showing");
            set_visible(b, !b->visible);
        }
    }
}

static void input_open(struct bd *b, const char *path)
{
    for (int i = 0; i < MAXIN; i++)
        if (b->in[i].fd >= 0 && !strcmp(b->in[i].path, path))
            return;
    int slot = -1;
    for (int i = 0; i < MAXIN; i++)
        if (b->in[i].fd < 0) {
            slot = i;
            break;
        }
    if (slot < 0)
        return;
    int fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0)
        return;
    unsigned long keys[KEY_MAX / (8 * sizeof(unsigned long)) + 1];
    memset(keys, 0, sizeof keys);
    if (ioctl(fd, EVIOCGBIT(EV_KEY, sizeof keys), keys) < 0 ||
        !((keys[KEY_ESC / (8 * sizeof(unsigned long))] >> (KEY_ESC % (8 * sizeof(unsigned long)))) & 1UL)) {
        close(fd);
        return;
    }
    struct indev *d = &b->in[slot];
    d->fd = fd;
    sfmt(d->path, sizeof d->path, "%s", path);
    if (ioctl(fd, EVIOCGNAME(sizeof d->name), d->name) < 0)
        sfmt(d->name, sizeof d->name, "keyboard");
    d->w = rb_loop_watch(b->loop, fd, EPOLLIN, input_ready, b);
    if (!b->initramfs)
        rb_log("keyboard %s: %s", path, d->name);
    b->dirty = true;
}

static void input_scan(struct bd *b)
{
    DIR *dir = opendir("/dev/input");
    if (!dir)
        return;
    struct dirent *e;
    while ((e = readdir(dir)))
        if (!strncmp(e->d_name, "event", 5)) {
            char p[64];
            sfmt(p, sizeof p, "/dev/input/%s", e->d_name);
            input_open(b, p);
        }
    closedir(dir);
}

/* ------------------------------------------------------------------------ */
/* kernel uevents                                                           */

static void tick(void *data);

static void uevent_ready(void *data, int fd, uint32_t events)
{
    (void)events;
    struct bd *b = data;
    char msg[8192];
    bool rescan = false, fb = false;
    for (;;) {
        ssize_t n = recv(fd, msg, sizeof msg - 1, MSG_DONTWAIT);
        if (n < 0) {
            if (errno == ENOBUFS) { /* missed some: look at everything again */
                rescan = fb = true;
                continue;
            }
            break;
        }
        msg[n] = 0;
        const char *action = NULL, *subsys = NULL, *devname = NULL, *devpath = NULL;
        for (char *p = msg; p < msg + n; p += strlen(p) + 1) {
            if (!strncmp(p, "ACTION=", 7))
                action = p + 7;
            else if (!strncmp(p, "SUBSYSTEM=", 10))
                subsys = p + 10;
            else if (!strncmp(p, "DEVNAME=", 8))
                devname = p + 8;
            else if (!strncmp(p, "DEVPATH=", 8))
                devpath = p + 8;
        }
        if (!action || !subsys)
            continue;
        if (!strcmp(subsys, "input") && devname && !strncmp(devname, "input/event", 11)) {
            if (!strcmp(action, "add")) {
                char p[64];
                sfmt(p, sizeof p, "/dev/%s", devname);
                input_open(b, p);
            } else if (!strcmp(action, "remove")) {
                char p[64];
                sfmt(p, sizeof p, "/dev/%s", devname);
                for (int i = 0; i < MAXIN; i++)
                    if (b->in[i].fd >= 0 && !strcmp(b->in[i].path, p))
                        input_close(b, i);
            }
        } else if ((!strcmp(subsys, "platform") && devpath && strstr(devpath, "framebuffer")) ||
                   (!strcmp(subsys, "graphics") && devname && !strcmp(devname, "fb0"))) {
            fb = true;
            if (!strcmp(action, "remove") && b->fb) {
                rb_log("firmware framebuffer device removed (a display driver takes over)");
                fb_unmap(b);
                b->shown_ok = false;
            }
        } else if (!strcmp(subsys, "drm") || !strcmp(subsys, "pci")) {
            if (!strcmp(subsys, "drm") && !strcmp(action, "add") && b->fb) {
                fb_unmap(b);
                b->shown_ok = false;
            }
            fb = true;
            b->dirty = true;
        } else if (!strcmp(subsys, "block") || !strcmp(subsys, "usb")) {
            b->dirty = true;
        }
    }
    if (rescan)
        input_scan(b);
    if (fb) {
        b->fb_nodrv_since = 0;
        rb_timer_arm(b->tick, 50);
    }
}

/* ------------------------------------------------------------------------ */
/* main loop                                                                */

static bool may_exit(struct bd *b, uint64_t now)
{
    if (b->initramfs || !b->st.done[BD_READY] || b->visible || b->save_pid > 0 || b->save_retry_at)
        return false;
    if (b->opened_ever && b->last_save_done_start < b->hide_ms)
        return false;
    /* The installer saves at 90 s and 180 s in any case: a frozen panel looks
     * ready to software (ARCHITECTURE 16). */
    if (b->cfg.installer && !b->auto2 && now < AUTOSAVE2_MS + 30000)
        return false;
    return true;
}

static void tick(void *data)
{
    struct bd *b = data;
    uint64_t now = now_ms();
    stages_eval(b);
    bool ready = b->st.done[BD_READY];
    /* the hint after 45 s without the installer window (or Mac OS) */
    bool hint = !ready && now >= HINT_MS;
    if (hint != b->hint) {
        if (hint) {
            int fd = open(RB_DIAG_HINT, O_WRONLY | O_CREAT | O_CLOEXEC, 0644);
            if (fd >= 0)
                close(fd);
        } else {
            unlink(RB_DIAG_HINT);
        }
        visible_sync(b);
    }
    /* reports */
    if (b->visible && now - b->last_save_start >= SAVE_EVERY_MS)
        save_start(b, "every 15 s while the window is open");
    bool unattended = !ready || b->cfg.installer;
    if (!b->auto1 && now >= AUTOSAVE1_MS && unattended) {
        b->auto1 = true;
        save_start(b, ready ? "90 s after power-on" : "no installer window 90 s after power-on");
    }
    if (!b->auto2 && now >= AUTOSAVE2_MS && unattended) {
        b->auto2 = true;
        save_start(b, ready ? "180 s after power-on" : "no installer window 180 s after power-on");
    }
    if (b->save_retry_at && now >= b->save_retry_at && b->save_pid <= 0) {
        b->save_retry_at = 0;
        save_start(b, b->save_reason[0] ? b->save_reason : "retry");
    }
    if (b->save_pid > 0 && now_ms() > b->save_started + SAVE_TIMEOUT_MS) {
        kill(b->save_pid, SIGKILL);
        save_finish(b, "err the USB stick did not answer");
    }
    if (b->visible && now - b->failed_ms > 10000)
        failed_units_update(b);
    if (b->visible && now - b->view_written_ms >= 5000)
        b->dirty = true; /* the clock and the hardware lists */
    /* the firmware framebuffer: follow the rules every tick (drivers come and go) */
    fb_draw(b);
    view_update(b, false);
    if (b->visible && b->fb_touched && b->fb_view_gen != b->doc_gen)
        fb_draw(b);
    if (may_exit(b, now)) {
        rb_log("startup finished; exiting");
        unlink(RB_DIAG_HINT);
        state_save(b);
        rb_loop_quit(b->loop, 0);
        return;
    }
    rb_timer_arm(b->tick, TICK_MS);
}

static void inotify_ready(void *data, int fd, uint32_t events)
{
    (void)events;
    struct bd *b = data;
    char ev[4096];
    bool pres = false;
    ssize_t n;
    while ((n = read(fd, ev, sizeof ev)) > 0)
        for (char *p = ev; p < ev + n;) {
            struct inotify_event *ie = (struct inotify_event *)p;
            if (ie->len && !strcmp(ie->name, "presenter"))
                pres = true;
            p += sizeof *ie + ie->len;
        }
    visible_sync(b);
    if (pres) {
        presenter_read(b);
        rb_timer_arm(b->tick, 20);
    }
}

static void signal_ready(void *data, int fd, uint32_t events)
{
    (void)events;
    struct bd *b = data;
    struct signalfd_siginfo si;
    while (read(fd, &si, sizeof si) == (ssize_t)sizeof si) {
        if (si.ssi_signo == SIGCHLD) {
            /* a save child is reaped in save_finish; anything else here */
            pid_t p;
            while ((p = waitpid(-1, NULL, WNOHANG)) > 0)
                if (p == b->save_pid && b->save_fd >= 0)
                    break; /* its result is still in the pipe */
            continue;
        }
        if (b->save_pid > 0 && b->save_fd >= 0) {
            /* let a running save report (bounded): the next instance shows it */
            for (int i = 0; i < 30; i++) {
                struct pollfd pf = {b->save_fd, POLLIN, 0};
                if (poll(&pf, 1, 50) > 0) {
                    save_done(b, b->save_fd, 0);
                    break;
                }
            }
        }
        rb_log("signal %d: saving state and exiting", (int)si.ssi_signo);
        state_save(b);
        rb_loop_quit(b->loop, 0);
    }
}

static int stop_initramfs_instance(void)
{
    /* init-bottom: the initramfs instance saves its state and exits before
     * switch_root; the service continues in the real root. */
    char buf[32];
    if (rb_diag_read_file(RB_DIAG_DIR "/pid", buf, sizeof buf) <= 0)
        return 0;
    int pid = atoi(buf);
    if (pid <= 1 || kill(pid, SIGTERM) < 0)
        return 0;
    for (int i = 0; i < 150; i++) {
        char path[64], st[256];
        sfmt(path, sizeof path, "/proc/%d/stat", pid);
        if (rb_diag_read_file(path, st, sizeof st) <= 0)
            return 0;
        const char *rp = strrchr(st, ')');
        if (rp && rp[1] == ' ' && rp[2] == 'Z')
            return 0;
        usleep(20 * 1000);
    }
    return 1;
}

int main(int argc, char **argv)
{
    struct bd *b = &B;
    bool initramfs = argc > 1 && !strcmp(argv[1], "--initramfs");
    if (argc > 1 && !strcmp(argv[1], "--initramfs-exit"))
        return stop_initramfs_instance();
    if (argc > 1 && (!strcmp(argv[1], "--help") || !strcmp(argv[1], "-h"))) {
        printf("usage: rb-bootdiag [--initramfs | --initramfs-exit]\n"
               "Startup Details (ARCHITECTURE 16): progress, Escape, the firmware framebuffer, reports.\n");
        return 0;
    }
    memset(b, 0, sizeof *b);
    b->initramfs = initramfs;
    b->kmsg_fd = b->nl_fd = b->ino_fd = b->save_fd = -1;
    b->fb_bar_pci = -1;
    for (int i = 0; i < MAXIN; i++)
        b->in[i].fd = -1;
    rb_diag_boot_cfg(&b->cfg);
    if (!b->cfg.enabled)
        return 0;
    /* In the initramfs stderr is the console; the kernel log at debug level
     * never reaches a screen (loglevel=0) and keeps the notes for later. */
    rb_log_init("rb-bootdiag", initramfs);
    if (argv && argv[0] && argv[0][0] && initramfs)
        argv[0][0] = '@'; /* left alone by systemd's switch-root killing spree, should it outlive init-bottom */
    signal(SIGPIPE, SIG_IGN);
    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGTERM);
    sigaddset(&mask, SIGINT);
    sigaddset(&mask, SIGCHLD);
    sigprocmask(SIG_BLOCK, &mask, NULL);
    mkdir("/run/retroboot", 0755);
    mkdir(RB_DIAG_DIR, 0755);
    char pid[16];
    sfmt(pid, sizeof pid, "%d\n", (int)getpid());
    rb_diag_write_atomic(RB_DIAG_DIR "/pid", pid, strlen(pid));
    b->start_ms = now_ms();
    read_trim("/proc/sys/kernel/random/boot_id", b->boot_id, sizeof b->boot_id);
    b->ring = calloc(RING, sizeof *b->ring);
    b->doc_text = malloc(64 * 1024);
    if (!b->ring || !b->doc_text)
        return 1;
    rb_diag_doc_init(&b->doc, NULL);
    facts_static(b); /* first: a device-tree board's firmware screen is /dev/fb0 */
    fb_setup(b);
    state_load(b);
    rb_log("start (%s, %s%s), %s", initramfs ? "initramfs" : "root", b->cfg.installer ? "installer" : "installed",
           b->cfg.fb ? "" : ", no framebuffer drawing", b->build);
    if (initramfs)
        stage_done(b, BD_KERNEL, b->start_ms, NULL);
    else if (!b->st.done[BD_KERNEL])
        stage_done(b, BD_KERNEL, 0, NULL);

    b->loop = rb_loop_new();
    if (!b->loop)
        return 1;
    int sfd = signalfd(-1, &mask, SFD_NONBLOCK | SFD_CLOEXEC);
    if (sfd >= 0)
        rb_loop_watch(b->loop, sfd, EPOLLIN, signal_ready, b);
    b->kmsg_fd = open("/dev/kmsg", O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (b->kmsg_fd >= 0) {
        kmsg_read(b, b->kmsg_fd, 0);
        rb_loop_watch(b->loop, b->kmsg_fd, EPOLLIN, kmsg_read, b);
    }
    b->nl_fd = socket(AF_NETLINK, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, NETLINK_KOBJECT_UEVENT);
    if (b->nl_fd >= 0) {
        struct sockaddr_nl sa = {.nl_family = AF_NETLINK, .nl_groups = 1};
        int sz = 1 << 20;
        setsockopt(b->nl_fd, SOL_SOCKET, SO_RCVBUFFORCE, &sz, sizeof sz);
        if (bind(b->nl_fd, (struct sockaddr *)&sa, sizeof sa) == 0)
            rb_loop_watch(b->loop, b->nl_fd, EPOLLIN, uevent_ready, b);
    }
    b->ino_fd = inotify_init1(IN_NONBLOCK | IN_CLOEXEC);
    if (b->ino_fd >= 0 &&
        inotify_add_watch(b->ino_fd, RB_DIAG_DIR, IN_CREATE | IN_DELETE | IN_MOVED_TO | IN_CLOSE_WRITE) >= 0)
        rb_loop_watch(b->loop, b->ino_fd, EPOLLIN, inotify_ready, b);
    input_scan(b);
    b->tick = rb_timer_new(b->loop, tick, b);
    b->hint = exists(RB_DIAG_HINT);
    stages_eval(b);
    if (b->cfg.show && !b->opened_ever && !exists(RB_DIAG_VISIBLE))
        set_visible(b, true);
    visible_sync(b);
    view_update(b, true);
    fb_draw(b);
    tick(b);
    while (rb_loop_running(b->loop))
        rb_loop_dispatch(b->loop, -1);
    if (b->save_pid > 0 && !b->initramfs) {
        /* let a running save finish (bounded) */
        for (int i = 0; i < 100 && waitpid(b->save_pid, NULL, WNOHANG) == 0; i++)
            usleep(50 * 1000);
    }
    unlink(RB_DIAG_DIR "/pid");
    return rb_loop_exit_code(b->loop);
}
