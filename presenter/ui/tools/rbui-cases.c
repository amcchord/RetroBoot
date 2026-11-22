/*
 * rbui-cases.c - draw Platinum controls at the coordinates of private Mac OS 9.2.1
 * reference captures, so web/dev/platinum_check.py --panel can compare the classic
 * panel's pixels with the guest's (the same cases as the web gallery).
 *
 *   RBUI_ASSETS=private/platinum/bundle rbui-cases OUTDIR [CASE...]
 *
 * Each case is a 1024 x 768 PNG, OUTDIR/<case>.png. Development tool, not installed.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "rbui_internal.h"

#define W 1024
#define H 768

typedef struct {
    const char *name;
    int bx, by, bw, bh;   /* backdrop (the reference's surroundings) */
    uint32_t bg;
    void (*draw)(rb_gfx *g, const rbf_font *sys);
} kase;

static void text(rb_gfx *g, const rbf_font *f, int x, int baseline, const char *s)
{
    tx_draw(g, f, 1, x, baseline, s, strlen(s), C_BLACK);
}

static void b_cancel(rb_gfx *g, const rbf_font *f) { pt_button(g, rr(521, 212, 80, 20), "Cancel", PT_NORMAL, 0, f); }
static void b_restart(rb_gfx *g, const rbf_font *f) { pt_button(g, rr(335, 212, 80, 20), "Restart", PT_NORMAL, 0, f); }
static void b_default(rb_gfx *g, const rbf_font *f) { pt_button(g, rr(614, 212, 80, 20), "Shut Down", PT_NORMAL, 1, f); }
static void b_pressed(rb_gfx *g, const rbf_font *f) { pt_button(g, rr(521, 212, 80, 20), "Cancel", PT_PRESSED, 0, f); }
static void b_def_pressed(rb_gfx *g, const rbf_font *f) { pt_button(g, rr(614, 212, 80, 20), "Shut Down", PT_PRESSED, 1, f); }
static void b_disabled(rb_gfx *g, const rbf_font *f)
{
    pt_button(g, rr(630, 403, 103, 20), "Save Theme\xE2\x80\xA6", PT_DISABLED, 0, f);
}

/* Checkbox and radio rows as render.c lays them out: box 2 pixels below the 16-pixel
 * line, label baseline 10 below the box top. */
static void check(rb_gfx *g, const rbf_font *f, int x, int y, int on, const char *label)
{
    pt_checkbox(g, x, y + 2, on, PT_NORMAL);
    text(g, f, x + pt_check_text_dx(), y + 12, label);
}
static void radio(rb_gfx *g, const rbf_font *f, int x, int y, int on, const char *label)
{
    pt_radio(g, x, y + 2, on, PT_NORMAL);
    text(g, f, x + pt_check_text_dx(), y + 12, label);
}
static void c_off(rb_gfx *g, const rbf_font *f) { check(g, f, 172, 67, 0, "Show files' region information"); }
static void c_on(rb_gfx *g, const rbf_font *f) { check(g, f, 171, 84, 1, "Show detailed device and volume information"); }
static void r_on(rb_gfx *g, const rbf_font *f) { radio(g, f, 170, 51, 1, "Apple System Profiler document"); }
static void r_off(rb_gfx *g, const rbf_font *f) { radio(g, f, 403, 51, 0, "Text document"); }

/* Pressed (General Controls, pointer down; the pointer is masked in the comparison). */
static void c_off_p(rb_gfx *g, const rbf_font *f) { (void)f; pt_checkbox(g, 103, 164, 0, PT_PRESSED); }
static void c_on_p(rb_gfx *g, const rbf_font *f) { (void)f; pt_checkbox(g, 103, 117, 1, PT_PRESSED); }
static void r_off_p(rb_gfx *g, const rbf_font *f) { (void)f; pt_radio(g, 106, 371, 0, PT_PRESSED); }
static void r_on_p(rb_gfx *g, const rbf_font *f) { (void)f; pt_radio(g, 106, 389, 1, PT_PRESSED); }

static void popup(rb_gfx *g, const rbf_font *f) { pt_popup(g, rr(400, 176, 180, 20), "Apple platinum", PT_NORMAL, f); }
static void popup_dis(rb_gfx *g, const rbf_font *f)
{
    pt_popup(g, rr(519, 370, 194, 20), "Position Automatically", PT_DISABLED, f);
}

static void field(rb_gfx *g, const rbf_font *f)
{
    rb_rect c = rr(657, 353, 26, 22);   /* the black line; the etch is outside */
    pt_field(g, c, 0, 0);
    text(g, f, c.x + pt_field_text_dx(), c.y + (c.h - f->cap) / 2 + f->cap, "12");
}

static void progress(rb_gfx *g, const rbf_font *f)
{
    (void)f;
    pt_progress(g, rr(383, 206, 186, 12), 54.0 / 184.0, 0, 0, 0);   /* 54 interior pixels filled */
}

static void group(rb_gfx *g, const rbf_font *f) { pt_group(g, rr(91, 85, 326, 120), "Desktop", f, 0); }

/* Appearance's desktop-pattern list (focused) with its scroll bar, thumb where the
 * capture has it; its arrows sit together at the bottom as in the capture. */
static void scrollbar(rb_gfx *g, const rbf_font *f)
{
    (void)f;
    rb_rect list = rr(600, 189, 113, 130);
    pt_listbox(g, list, 0, 1);
    pt_scrollbar(g, rr(list.x + list.w - 16, list.y, 16, list.h), 100, 10, 0, 0, 243);
}

/* The Shut Down alert's frame (its contents are compared by the web's alert case). */
static void alert(rb_gfx *g, const rbf_font *f) { pt_window(g, rr(310, 148, 403, 103), NULL, 0, f); }

/* Apple System Profiler's New Report: a movable modal dialog, title and frame. */
static void movable(rb_gfx *g, const rbf_font *f) { pt_window(g, rr(311, 161, 401, 324), "New Report", 1, f); }

static const kase kCases[] = {
    {"button-cancel", 500, 200, 200, 44, 0xDDDDDD, b_cancel},
    {"button-restart", 320, 200, 200, 44, 0xDDDDDD, b_restart},
    {"button-default", 600, 200, 110, 44, 0xDDDDDD, b_default},
    {"button-pressed", 500, 200, 200, 44, 0xDDDDDD, b_pressed},
    {"button-default-pressed", 600, 200, 110, 44, 0xDDDDDD, b_def_pressed},
    {"button-disabled", 600, 395, 150, 36, 0xEEEEEE, b_disabled},
    {"checkbox-off", 160, 64, 300, 20, 0xDDDDDD, c_off},
    {"checkbox-on", 160, 81, 330, 20, 0xDDDDDD, c_on},
    {"radio-on", 160, 48, 240, 20, 0xDDDDDD, r_on},
    {"radio-off", 395, 48, 140, 20, 0xDDDDDD, r_off},
    {"checkbox-off-pressed", 96, 158, 60, 24, 0xDDDDDD, c_off_p},
    {"checkbox-on-pressed", 96, 111, 60, 24, 0xDDDDDD, c_on_p},
    {"radio-off-pressed", 96, 365, 60, 24, 0xDDDDDD, r_off_p},
    {"radio-on-pressed", 96, 383, 60, 24, 0xDDDDDD, r_on_p},
    {"popup", 390, 170, 210, 30, 0xEEEEEE, popup},
    {"popup-disabled", 510, 365, 210, 30, 0xEEEEEE, popup_dis},
    {"field", 650, 345, 40, 34, 0xEEEEEE, field},
    {"progress-54", 375, 198, 200, 26, 0xDDDDDD, progress},
    {"group-desktop", 86, 80, 340, 130, 0xDDDDDD, group},
    {"scrollbar", 590, 180, 140, 150, 0xEEEEEE, scrollbar},
    {"alert-frame", 300, 140, 420, 120, 0x63639C, alert},
    {"movable", 300, 150, 430, 350, 0xEEEEEE, movable},
};

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: RBUI_ASSETS=BUNDLE rbui-cases OUTDIR [CASE...]\n");
        return 2;
    }
    rbui *ui = rbui_new();   /* loads $RBUI_ASSETS */
    if (!ui) return 1;
    if (!pt_v9()) fprintf(stderr, "rbui-cases: no Platinum bundle controls loaded; drawing the 8.1 look\n");
    const rbf_font *sys = rbf_get("system12");
    uint32_t *px = malloc((size_t)W * H * sizeof(uint32_t));
    if (!px) return 1;
    int done = 0;
    for (size_t i = 0; i < sizeof(kCases) / sizeof(kCases[0]); ++i) {
        const kase *k = &kCases[i];
        int want = argc == 2;
        for (int a = 2; a < argc; ++a) want |= strcmp(argv[a], k->name) == 0;
        if (!want) continue;
        rb_gfx g = {px, W, W, H, rr(0, 0, W, H), 0, 0};
        g_fill(&g, 0, 0, W, H, 0x63639C);
        g_fill(&g, k->bx, k->by, k->bw, k->bh, k->bg);
        k->draw(&g, sys);
        char path[1024];
        snprintf(path, sizeof(path), "%s/%s.png", argv[1], k->name);
        if (rbui_write_png(path, px, W, H, W) != 0) {
            fprintf(stderr, "rbui-cases: can't write %s\n", path);
            return 1;
        }
        ++done;
    }
    free(px);
    rbui_free(ui);
    return done ? 0 : 1;
}
