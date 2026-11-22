/* rb-bootdiag's pure parts (bootdiag.h): kernel log records and problem
 * ranking, stage notes, the progress contract, the firmware framebuffer's
 * description and pixel formats, report names and pruning, FAT labels. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "bootdiag.h"
#include "rbtest.h"

static void kmsg_tests(void)
{
    struct bd_kmsg k;
    const char r1[] = "4,1234,12345678,-;amdgpu 0000:c4:00.0: Direct firmware load for amdgpu/gc_11_5_0_mes_2.bin "
                      "failed with error -2\n SUBSYSTEM=pci\n DEVICE=+pci:0000:c4:00.0\n";
    CHECK_EQ(bd_kmsg_parse(r1, sizeof r1 - 1, &k), 0);
    CHECK_EQ(k.prio, 4);
    CHECK_EQ(k.seq, 1234);
    CHECK_EQ(k.ts_us, 12345678);
    CHECK(!strcmp(k.text, "amdgpu 0000:c4:00.0: Direct firmware load for amdgpu/gc_11_5_0_mes_2.bin failed with error -2"));
    /* facility bits, caller field, escaped bytes */
    const char r2[] = "14,7,900,-,caller=T1;retroboot: stage medium-found /dev/sda\\x0a\n";
    CHECK_EQ(bd_kmsg_parse(r2, sizeof r2 - 1, &k), 0);
    CHECK_EQ(k.prio, 6);
    CHECK(!strcmp(k.text, "retroboot: stage medium-found /dev/sda"));
    CHECK(bd_kmsg_parse("garbage", 7, &k) < 0);

    /* ranking */
    int fw = bd_problem_score(4, "amdgpu 0000:c4:00.0: Direct firmware load for amdgpu/x.bin failed with error -2");
    int probe = bd_problem_score(4, "amdgpu: probe of 0000:c4:00.0 failed with error -2");
    int probe2 = bd_problem_score(3, "amdgpu 0000:c4:00.0: probe with driver amdgpu failed with error -22");
    int drm = bd_problem_score(3, "[drm:amdgpu_device_init [amdgpu]] *ERROR* sw_init of IP block <gfx_v11_0> failed -22");
    int gen = bd_problem_score(3, "usb 3-1: device descriptor read/64, error -71");
    int acpi = bd_problem_score(3, "ACPI BIOS Error (bug): Could not resolve symbol [\\_SB.PCI0], AE_NOT_FOUND");
    int info = bd_problem_score(6, "usb 3-1: new high-speed USB device number 2 using xhci_hcd");
    int warn = bd_problem_score(4, "x86/cpu: SGX disabled by BIOS.");
    int ours = bd_problem_score(3, "rb-splash: [mono 1.0 boot 1.0] splash: error");
    int panic = bd_problem_score(6, "retroboot: panic installer medium not found");
    CHECK(panic > fw && fw >= probe && probe == probe2 && probe > drm && drm > gen && gen > acpi && acpi > 0);
    CHECK_EQ(info, 0);
    CHECK_EQ(warn, 0);
    CHECK_EQ(ours, 0);
    CHECK(bd_problem_score(4, "WARNING: CPU: 3 PID: 1 at drivers/gpu/drm/amd/display/dc/core/dc.c:123") >= 70);
    /* a frozen panel: amdgpu's flip and commit timeouts lead, then DMUB/PSR and ring timeouts */
    int flip = bd_problem_score(3, "[drm:amdgpu_dm_atomic_commit_tail [amdgpu]] *ERROR* [CRTC:79:crtc-0] flip_done timed out");
    int cwait = bd_problem_score(3, "[drm:drm_atomic_helper_wait_for_dependencies] *ERROR* [CRTC:79:crtc-0] commit wait timed out");
    int ring = bd_problem_score(3, "amdgpu 0000:c4:00.0: amdgpu: ring gfx_0.0.0 timeout, signaled seq=1, emitted seq=3");
    int dmub = bd_problem_score(3, "amdgpu 0000:c4:00.0: [drm] *ERROR* Error waiting for DMUB idle: status=3");
    int psr = bd_problem_score(4, "amdgpu 0000:c4:00.0: [drm] PSR: failed to enable");
    CHECK(flip == 95 && cwait == 95);
    CHECK(flip > ring && ring > fw && dmub == 88 && psr == 88 && dmub > drm);
    CHECK_EQ(bd_problem_score(6, "[drm] PSR support 1, DC PSR ver 0, sink PSR ver 3"), 0);
    /* a program's crash ranks below the kernel's own */
    CHECK(bd_problem_score(6, "traps: perf[1] general protection fault ip:5 sp:7 error:0 in perf") <
          bd_problem_score(4, "general protection fault, probably for non-canonical address 0xdead: 0000 [#1] SMP"));

    /* stage notes */
    char id[32], det[128];
    CHECK(bd_marker("retroboot: stage medium-found /dev/sda", id, sizeof id, det, sizeof det));
    CHECK(!strcmp(id, "medium-found") && !strcmp(det, "/dev/sda"));
    CHECK(bd_marker("retroboot: stage switch-root", id, sizeof id, det, sizeof det));
    CHECK(!strcmp(id, "switch-root") && !det[0]);
    CHECK(bd_marker("retroboot: panic installer medium not found", id, sizeof id, det, sizeof det));
    CHECK(!strcmp(id, "panic") && !strcmp(det, "installer medium not found"));
    CHECK(!bd_marker("retroboot-live: something", id, sizeof id, det, sizeof det));
    CHECK_EQ(bd_stage_from_id("display-driver"), BD_DISPLAY);
    CHECK_EQ(bd_stage_from_id("nope"), -1);
}

static void progress_tests(void)
{
    struct bd_stages st;
    int s;
    memset(&st, 0, sizeof st);
    CHECK_EQ(bd_progress(&st, &s), 0);
    st.done[BD_KERNEL] = true;
    CHECK_EQ(bd_progress(&st, &s), 50);
    CHECK_EQ(s, BD_KERNEL);
    /* bochs binds in the initramfs before the medium is found: no jump */
    st.done[BD_DISPLAY] = true;
    CHECK_EQ(bd_progress(&st, &s), 50);
    st.done[BD_MEDIUM_SEARCH] = true;
    CHECK_EQ(bd_progress(&st, &s), 100);
    st.done[BD_MEDIUM_FOUND] = true;
    CHECK_EQ(bd_progress(&st, &s), 250);
    st.done[BD_SQUASHFS] = true; /* checklist only */
    CHECK_EQ(bd_progress(&st, &s), 250);
    st.done[BD_ROOT] = true;
    CHECK_EQ(bd_progress(&st, &s), 350);
    st.done[BD_SWITCH_ROOT] = true;
    CHECK_EQ(bd_progress(&st, &s), 550); /* the display was already there */
    CHECK_EQ(s, BD_DISPLAY);
    st.done[BD_PRESENTER] = st.done[BD_HOSTD] = true;
    CHECK_EQ(bd_progress(&st, &s), 800);
    CHECK(!strcmp(bd_stage_id(s), "hostd"));
    st.done[BD_READY] = true;
    CHECK_EQ(bd_progress(&st, &s), 1000);
    CHECK(!strcmp(bd_stage_id(s), "ready"));

    /* the AMD failure: no display driver ever; the presenter and hostd run */
    memset(&st, 0, sizeof st);
    st.done[BD_KERNEL] = st.done[BD_MEDIUM_SEARCH] = st.done[BD_MEDIUM_FOUND] = st.done[BD_ROOT] = true;
    st.done[BD_SWITCH_ROOT] = st.done[BD_PRESENTER] = st.done[BD_HOSTD] = true;
    st.failed[BD_DISPLAY] = true;
    CHECK_EQ(bd_progress(&st, &s), 450);
    CHECK_EQ(s, BD_SWITCH_ROOT);
    /* a later stage implies the earlier ones (a missed note) */
    memset(&st, 0, sizeof st);
    st.done[BD_KERNEL] = st.done[BD_PRESENTER] = true;
    CHECK_EQ(bd_progress(&st, &s), 450);
    /* never decreasing */
    st.published = 800;
    CHECK_EQ(bd_progress(&st, &s), 800);
    CHECK_EQ(s, BD_HOSTD);
    /* the installed system skips the medium stages */
    memset(&st, 0, sizeof st);
    st.skipped[BD_MEDIUM_SEARCH] = st.skipped[BD_MEDIUM_FOUND] = st.skipped[BD_SQUASHFS] = true;
    st.done[BD_KERNEL] = st.done[BD_ROOT] = true;
    CHECK_EQ(bd_progress(&st, &s), 350);

    char line[160];
    bd_progress_line(250, BD_MEDIUM_FOUND, "Installer disk found", line, sizeof line);
    CHECK(!strcmp(line, "250 medium-found Installer disk found\n"));
    /* text at most 80 bytes, never a broken UTF-8 sequence, never a newline */
    char long_text[200];
    memset(long_text, 'a', 79);
    strcpy(long_text + 79, "\xe2\x80\xa6 and more\nx");
    bd_progress_line(1200, BD_READY, long_text, line, sizeof line);
    CHECK(!strncmp(line, "1000 ready ", 11));
    CHECK_EQ(strlen(line), 11 + 79 + 1);
    bd_progress_line(-5, -1, "a\nb", line, sizeof line);
    CHECK(!strcmp(line, "0 kernel a b\n"));
}

static void fb_tests(void)
{
    struct bd_fbinfo fi;
    /* OVMF, VIDEO_TYPE_EFI 1280x800x32 (the bytes test_bootfb uses) */
    static const uint8_t ovmf[0x40] = {0x00, 0x00, 0x00, 0x80, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x70,
                                       0x10, 0x00, 0x00, 0x05, 0x20, 0x03, 0x20, 0x00, 0, 0, 0, 0xfd, 0, 0,
                                       0x3f, 0x00, 0x3f, 0xa3, 0x00, 0x10, 0x00, 0x14, 0x08, 0x10, 0x08, 0x08,
                                       0x08, 0x00, 0x08, 0x18};
    CHECK_EQ(bd_screen_info(ovmf, sizeof ovmf, &fi), 0);
    CHECK_EQ(fi.w, 1280);
    CHECK_EQ(fi.h, 800);
    CHECK_EQ(fi.depth, 32);
    CHECK_EQ(fi.pitch, 5120);
    CHECK_EQ(fi.base, 0xfd000000ull);
    CHECK(fi.rpos == 16 && fi.gpos == 8 && fi.bpos == 0 && fi.rsize == 8);
    /* a 64-bit base (an APU's BAR above 4 GB) */
    uint8_t b[0x40];
    memcpy(b, ovmf, sizeof b);
    b[0x36] = 2;
    b[0x3a] = 0x80;
    CHECK_EQ(bd_screen_info(b, sizeof b, &fi), 0);
    CHECK_EQ(fi.base, 0x80fd000000ull);
    /* without the capability bit the high half is ignored */
    b[0x36] = 0;
    CHECK_EQ(bd_screen_info(b, sizeof b, &fi), 0);
    CHECK_EQ(fi.base, 0xfd000000ull);
    /* 16 bpp with no colour layout: RGB565 */
    memset(b, 0, sizeof b);
    b[0x0f] = 0x23;
    b[0x12] = 0x00, b[0x13] = 0x04; /* 1024 */
    b[0x14] = 0x00, b[0x15] = 0x03; /* 768 */
    b[0x16] = 16;
    b[0x1b] = 0xe0;
    b[0x24] = 0x00, b[0x25] = 0x08; /* 2048 */
    CHECK_EQ(bd_screen_info(b, sizeof b, &fi), 0);
    CHECK(fi.rpos == 11 && fi.rsize == 5 && fi.gsize == 6 && fi.bsize == 5);
    /* rejected: text mode, a short pitch, no base, short buffer */
    b[0x0f] = 0x01;
    CHECK(bd_screen_info(b, sizeof b, &fi) < 0);
    b[0x0f] = 0x23;
    b[0x24] = 0x10, b[0x25] = 0x00;
    CHECK(bd_screen_info(b, sizeof b, &fi) < 0);
    CHECK(bd_screen_info(b, 0x10, &fi) < 0);

    /* pixel formats round-trip */
    uint32_t src[5] = {0x000000, 0xffffff, 0xcc0000, 0x007700, 0x123456}, back[5];
    uint8_t row[5 * 4];
    struct bd_fbinfo f32 = {0x70, 5, 1, 32, 20, 1, 16, 8, 8, 8, 0, 8};
    bd_fb_convert_row(src, row, 5, &f32);
    CHECK(!memcmp(row, src, sizeof src)); /* XRGB8888 is a copy */
    bd_fb_decode_row(row, back, 5, &f32);
    CHECK(!memcmp(back, src, sizeof src));
    struct bd_fbinfo bgr = {0x70, 5, 1, 32, 20, 1, 0, 8, 8, 8, 16, 8}; /* BGRX */
    bd_fb_convert_row(src, row, 5, &bgr);
    CHECK(row[8] == 0xcc && row[9] == 0x00 && row[10] == 0x00); /* red in the first byte */
    bd_fb_decode_row(row, back, 5, &bgr);
    CHECK(!memcmp(back, src, sizeof src));
    struct bd_fbinfo f24 = {0x23, 5, 1, 24, 15, 1, 16, 8, 8, 8, 0, 8};
    bd_fb_convert_row(src, row, 5, &f24);
    CHECK(row[3] == 0xff && row[4] == 0xff && row[5] == 0xff);
    bd_fb_decode_row(row, back, 5, &f24);
    CHECK(!memcmp(back, src, sizeof src));
    struct bd_fbinfo f16 = {0x23, 5, 1, 16, 10, 11, 5, 5, 6, 0, 5, 0};
    f16.rpos = 11, f16.rsize = 5, f16.gpos = 5, f16.gsize = 6, f16.bpos = 0, f16.bsize = 5;
    bd_fb_convert_row(src, row, 5, &f16);
    CHECK(row[2] == 0xff && row[3] == 0xff);
    bd_fb_decode_row(row, back, 5, &f16);
    CHECK_EQ(back[0], 0x000000);
    CHECK_EQ(back[1], 0xffffff); /* black and white are exact */
}

static void report_tests(void)
{
    char name[64];
    bd_report_name(1792185243, "1a2b3c4d-5e6f-7a8b-9c0d-112233445566", name, sizeof name);
    CHECK(!strcmp(name, "20261016-211403-1a2b3c4d.txt"));
    CHECK(bd_report_name_ok(name));
    CHECK(!bd_report_name_ok("network.conf"));
    CHECK(!bd_report_name_ok("20261016-211403-1a2b3c4g.txt"));
    CHECK(!bd_report_name_ok("2026101-2114030-1a2b3c4d.txt"));

    char names[13][64];
    for (int i = 0; i < 13; i++)
        snprintf(names[i], 64, "202610%02d-120000-0000000%d.txt", 10 + (i * 7) % 13, i % 10);
    int del[13];
    const char *own = names[5]; /* 20261019...: neither oldest nor newest */
    int k = bd_prune(names, 13, 10, own, del);
    CHECK_EQ(k, 3);
    for (int i = 0; i < k; i++) {
        CHECK(strcmp(names[del[i]], own));
        /* every deleted name is older than every kept one (except own) */
        for (int j = 0; j < 13; j++) {
            bool deleted = false;
            for (int m = 0; m < k; m++)
                deleted |= del[m] == j;
            if (!deleted && strcmp(names[j], own))
                CHECK(strcmp(names[del[i]], names[j]) < 0);
        }
    }
    CHECK_EQ(bd_prune(names, 10, 10, NULL, del), 0);
    /* own is the oldest: the next oldest go instead */
    char few[3][64] = {"20200101-000000-00000000.txt", "20200102-000000-00000000.txt", "20200103-000000-00000000.txt"};
    k = bd_prune(few, 3, 2, few[0], del);
    CHECK_EQ(k, 1);
    CHECK_EQ(del[0], 1);
}

static void fat_tests(void)
{
    uint8_t bs[512];
    char label[16];
    memset(bs, 0, sizeof bs);
    bs[510] = 0x55, bs[511] = 0xaa;
    memcpy(bs + 0x36, "FAT12   ", 8);
    bs[0x26] = 0x29;
    memcpy(bs + 0x2b, "RBINSTEFI  ", 11);
    CHECK_EQ(bd_fat_label(bs, sizeof bs, label, sizeof label), 0);
    CHECK(!strcmp(label, "RBINSTEFI"));
    memset(bs + 0x26, 0, 0x30);
    memcpy(bs + 0x52, "FAT32   ", 8);
    bs[0x42] = 0x29;
    memcpy(bs + 0x47, "RBEFI      ", 11);
    CHECK_EQ(bd_fat_label(bs, sizeof bs, label, sizeof label), 0);
    CHECK(!strcmp(label, "RBEFI"));
    bs[510] = 0;
    CHECK(bd_fat_label(bs, sizeof bs, label, sizeof label) < 0);
    char t[16];
    bd_fmt_secs(2450, t, sizeof t);
    CHECK(!strcmp(t, "2.4 s"));
    bd_fmt_secs(912345, t, sizeof t);
    CHECK(!strcmp(t, "912 s"));
    CHECK(!strcmp(bd_pci_vendor(0x1002), "AMD"));
    CHECK(bd_pci_vendor(0xffff) == NULL);
}

static void panel_tests(void)
{
    /* a synthetic laptop panel EDID: maker SDC, product 0x4171, 30 x 19 cm, a
     * text descriptor with the model and a name descriptor */
    uint8_t ed[128];
    memset(ed, 0, sizeof ed);
    static const uint8_t magic[8] = {0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0};
    memcpy(ed, magic, 8);
    unsigned v = (('S' - '@') << 10) | (('D' - '@') << 5) | ('C' - '@');
    ed[8] = (uint8_t)(v >> 8), ed[9] = (uint8_t)v;
    ed[10] = 0x71, ed[11] = 0x41;
    ed[21] = 30, ed[22] = 19;
    ed[54] = 0x12; /* a detailed timing (not a descriptor) */
    memcpy(ed + 72, "\0\0\0\xfe\0ATNA33AA02-0\n", 18);
    memcpy(ed + 90, "\0\0\0\xfc\0Panel One\n   ", 18);
    char t[160];
    CHECK_EQ(bd_edid_ident(ed, sizeof ed, t, sizeof t), 0);
    CHECK(!strcmp(t, "SDC 4171 \"ATNA33AA02-0\" \"Panel One\", 30x19 cm"));
    ed[1] = 0;
    CHECK(bd_edid_ident(ed, sizeof ed, t, sizeof t) < 0);
    CHECK(bd_edid_ident(ed, 64, t, sizeof t) < 0);
    bd_dcdebugmask_text(0x410, t, sizeof t);
    CHECK(!strcmp(t, "PSR off, Panel Replay off"));
    bd_dcdebugmask_text(0x610, t, sizeof t);
    CHECK(!strcmp(t, "PSR off, PSR-SU off, Panel Replay off"));
    bd_dcdebugmask_text(0, t, sizeof t);
    CHECK(!strcmp(t, "defaults"));
}

/* Raspberry Pi: revision codes, boot modes, cores, simplefb, USB storage without a driver. */
static void write_text(const char *dir, const char *name, const char *text)
{
    char path[512];
    snprintf(path, sizeof path, "%s/%s", dir, name);
    FILE *f = fopen(path, "w");
    if (f) {
        fputs(text, f);
        fclose(f);
    }
}

static void pi_tests(void)
{
    char t[200];
    CHECK_EQ(bd_pi_revision(0xc03115, t, sizeof t), 4096);
    CHECK(!strcmp(t, "Raspberry Pi 4 Model B rev 1.5, BCM2711, 4 GB (revision c03115)"));
    CHECK_EQ(bd_pi_revision(0xb03114, t, sizeof t), 2048);
    CHECK(!strcmp(t, "Raspberry Pi 4 Model B rev 1.4, BCM2711, 2 GB (revision b03114)"));
    CHECK_EQ(bd_pi_revision(0xd04170, t, sizeof t), 8192);
    CHECK(!strcmp(t, "Raspberry Pi 5 rev 1.0, BCM2712, 8 GB (revision d04170)"));
    CHECK_EQ(bd_pi_revision(0xa03111, t, sizeof t), 1024);
    CHECK_EQ(bd_pi_revision(0x000e, t, sizeof t), -1); /* old style */
    CHECK(!strcmp(bd_pi_boot_mode(1), "SD card"));
    CHECK(!strcmp(bd_pi_boot_mode(4), "USB mass storage"));
    CHECK(!strcmp(bd_pi_boot_mode(6), "NVMe"));
    CHECK(bd_pi_boot_mode(0xe) == NULL);
    CHECK(!strcmp(bd_arm_core(0x41, 0xd08), "Cortex-A72"));
    CHECK(!strcmp(bd_arm_core(0x41, 0xd0b), "Cortex-A76"));
    CHECK(bd_arm_core(0x51, 0xd08) == NULL);

    /* simplefb on a Pi 4: 32 bpp XRGB, as the firmware sets it up */
    struct bd_fbdev v = {.w = 1920, .h = 1080, .bpp = 32, .pitch = 7680, .roff = 16, .rlen = 8, .goff = 8,
                         .glen = 8, .boff = 0, .blen = 8, .base = 0x3e3c1000};
    struct bd_fbinfo fi;
    CHECK_EQ(bd_fbdev_info(&v, &fi), 0);
    CHECK_EQ(fi.type, BD_FB_FBDEV);
    CHECK(bd_firmware_fbdev("simple") && bd_firmware_fbdev("BCM2708 FB") && !bd_firmware_fbdev("vc4drmfb") &&
          !bd_firmware_fbdev(NULL));
    CHECK(fi.w == 1920 && fi.h == 1080 && fi.depth == 32 && fi.pitch == 7680 && fi.base == 0x3e3c1000);
    uint32_t px[2] = {0x00ff8040, 0x00123456};
    uint8_t row[8];
    bd_fb_convert_row(px, row, 2, &fi);
    CHECK(!memcmp(row, "\x40\x80\xff\x00\x56\x34\x12\x00", 8));
    /* RGB565 */
    struct bd_fbdev v16 = {.w = 800, .h = 480, .bpp = 16, .pitch = 1600, .roff = 11, .rlen = 5, .goff = 5,
                           .glen = 6, .boff = 0, .blen = 5};
    CHECK_EQ(bd_fbdev_info(&v16, &fi), 0);
    uint32_t back[2];
    bd_fb_convert_row(px, row, 2, &fi);
    bd_fb_decode_row(row, back, 2, &fi);
    CHECK_EQ(back[0], 0x00ff8242u);
    struct bd_fbdev bad = v;
    bad.pitch = 100;
    CHECK(bd_fbdev_info(&bad, &fi) < 0);
    bad = v;
    bad.bpp = 8;
    CHECK(bd_fbdev_info(&bad, &fi) < 0);
    bad = v;
    bad.roff = 30;
    CHECK(bd_fbdev_info(&bad, &fi) < 0);

    /* a USB stick whose storage interface no driver took, one that usb-storage has,
     * a keyboard (class 03) */
    char root[] = "/tmp/rb-bootdiag-usb-XXXXXX";
    CHECK(mkdtemp(root) != NULL);
    const char *dirs[] = {"1-1", "1-1:1.0", "1-2", "1-2:1.0", "1-2:1.0/driver", "1-3", "1-3:1.0", "2-1", "2-1:1.0",
                          "2-1:1.1"};
    for (size_t i = 0; i < sizeof dirs / sizeof dirs[0]; i++) {
        char p[512];
        snprintf(p, sizeof p, "%s/%s", root, dirs[i]);
        mkdir(p, 0755);
    }
    char p[512];
    snprintf(p, sizeof p, "%s/1-1", root);
    write_text(p, "idVendor", "0781\n"), write_text(p, "idProduct", "5583\n");
    write_text(p, "manufacturer", "SanDisk\n"), write_text(p, "product", "Ultra Fit\n");
    snprintf(p, sizeof p, "%s/1-1:1.0", root);
    write_text(p, "bInterfaceClass", "08\n");
    snprintf(p, sizeof p, "%s/1-2:1.0", root);
    write_text(p, "bInterfaceClass", "08\n");
    snprintf(p, sizeof p, "%s/1-3:1.0", root);
    write_text(p, "bInterfaceClass", "03\n");
    snprintf(p, sizeof p, "%s/2-1", root);
    write_text(p, "idVendor", "174c\n"), write_text(p, "idProduct", "55aa\n");
    snprintf(p, sizeof p, "%s/2-1:1.0", root);
    write_text(p, "bInterfaceClass", "08\n");
    snprintf(p, sizeof p, "%s/2-1:1.1", root);
    write_text(p, "bInterfaceClass", "08\n");
    char out[4][128];
    int n = bd_usb_storage_unbound(root, out, 4);
    CHECK_EQ(n, 2);
    int sandisk = !strcmp(out[0], "1-1 0781:5583 SanDisk Ultra Fit") || !strcmp(out[1], "1-1 0781:5583 SanDisk Ultra Fit");
    int asm1 = !strcmp(out[0], "2-1 174c:55aa") || !strcmp(out[1], "2-1 174c:55aa");
    CHECK(sandisk && asm1);
    CHECK_EQ(bd_usb_storage_unbound("/nonexistent-usb", out, 4), 0);
    char cmd[600];
    snprintf(cmd, sizeof cmd, "rm -rf '%s'", root);
    CHECK_EQ(system(cmd), 0);

    /* the kernel's Pi messages rank */
    CHECK(bd_problem_score(2, "hwmon hwmon1: Undervoltage detected!") >= 70);
    CHECK(bd_problem_score(3, "vc4-drm gpu: [drm] *ERROR* Failed to get HDMI state machine clock") >= 80);
    CHECK(bd_problem_score(3, "vc4_hdmi fef00700.hdmi: ASoC: error at snd_soc_dai_startup") >= 80);
}

int main(void)
{
    pi_tests();
    panel_tests();
    kmsg_tests();
    progress_tests();
    fb_tests();
    report_tests();
    fat_tests();
    TEST_DONE();
}
