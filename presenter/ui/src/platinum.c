/*
 * platinum.c - Mac OS 8/9 "Platinum" control drawing
 *
 * Provenance: the window frame, title pinstripes, push button / default ring
 * edge tables, checkbox and radio sprites, group box, edit field and list
 * frames and the scroll bar (lavender thumb) are ported from the user's own
 * M5Tab-Macintosh project (src/basilisk/preboot_ui.cpp), where they were
 * measured pixel by pixel from Mac OS 8.1. Popup button, progress bar, focus
 * ring, menu and the remaining pieces are original RetroBoot drawings in the
 * same palette.
 *
 * When the Platinum bundle built from the user's own Mac OS disk is present
 * (assets.c), every control is drawn from its Mac OS 9.2.1 sprites instead,
 * with 9.2.1's colours and geometry, so the panel matches the web client;
 * nothing Apple-derived is compiled in. The 8.1 tables remain the fallback.
 */
#include <stdio.h>
#include <string.h>

#include "rbui_internal.h"

/* ---- Look: Mac OS 8.1 tables or the bundle's Mac OS 9.2.1 sprites ---------- */

static const rb_pal kPal81 = {C_E6, C_BLACK, C_DISABLED, C_TEXT_DIM, C_B5, C_A5, C_HILITE, C_DE, C_MENU_HI,
                              C_WHITE, C_F7};
rb_pal g_pal = {C_E6, C_BLACK, C_DISABLED, C_TEXT_DIM, C_B5, C_A5, C_HILITE, C_DE, C_MENU_HI, C_WHITE, C_F7};
static int g_v9;
static uint32_t g_ramp[8];   /* the variation colours (Lavender), light to dark */

/* All of these, or none: a partial bundle keeps the 8.1 look throughout. */
static const char *const kNeed[] = {
    "movable", "alert", "button-normal", "button-pressed", "button-disabled", "button-default",
    "button-default-pressed", "checkbox-off", "checkbox-on", "checkbox-off-pressed", "checkbox-on-pressed",
    "checkbox-off-disabled", "checkbox-on-disabled", "radio-off", "radio-on", "radio-off-pressed",
    "radio-on-pressed", "radio-off-disabled", "radio-on-disabled", "popup", "popup-disabled", "field",
    "field-focus", "group", "progress", "progress-fill", "scroll-track-v", "scroll-thumb-v", "scroll-grip-v",
    "scroll-up", "scroll-down", "menu",
};

void pt_theme_update(void)
{
    uint32_t hi = 0;
    g_v9 = assets_ramp(g_ramp, &hi);
    for (size_t i = 0; g_v9 && i < sizeof(kNeed) / sizeof(kNeed[0]); ++i)
        if (!assets_sprite(kNeed[i])) g_v9 = 0;
    if (!g_v9) {
        g_pal = kPal81;
        return;
    }
    /* Mac OS 9.2.1 dialog face, disabled and secondary text as the web client draws them. */
    rb_pal p = {0xDDDDDDu, C_BLACK, 0x777777u, 0x555555u, 0x999999u, 0x888888u, hi, 0xCCCCCCu, g_ramp[4],
                C_WHITE, 0xEEEEEEu};
    g_pal = p;
}

int pt_v9(void) { return g_v9; }

/* Source index for target index i of n, keeping a pixels at the start and b at the end of
 * an sn-pixel sprite and repeating the middle. */
static int map9(int i, int n, int a, int b, int sn)
{
    if (i < a) return i < sn ? i : sn - 1;
    if (i >= n - b) {
        int k = sn - (n - i);
        return k < 0 ? 0 : k;
    }
    int mid = sn - a - b;
    return mid > 0 ? a + (i - a) % mid : (a < sn ? a : sn - 1);
}

static void nine_s(rb_gfx *g, const rb_sprite *s, rb_rect r, int t, int rt, int b, int l)
{
    if (!s || r.w <= 0 || r.h <= 0) return;
    for (int y = 0; y < r.h; ++y) {
        const uint32_t *row = s->argb + (size_t)map9(y, r.h, t, b, s->h) * (size_t)s->w;
        for (int x = 0; x < r.w; ++x) {
            uint32_t c = row[map9(x, r.w, l, rt, s->w)];
            if (c >> 24) g_blend(g, r.x + x, r.y + y, c);
        }
    }
}

/* A bundle sprite stretched to r by its manifest slices (the middles repeat). */
static void nine(rb_gfx *g, const char *name, rb_rect r)
{
    const rb_sprite *s = assets_sprite(name);
    if (s) nine_s(g, s, r, s->slice[0], s->slice[1], s->slice[2], s->slice[3]);
}

static void blit(rb_gfx *g, const char *name, int x, int y)
{
    const rb_sprite *s = assets_sprite(name);
    if (s) nine_s(g, s, rr(x, y, s->w, s->h), 0, 0, 0, 0);
}

/* One row of a sprite at (x, y). */
static void sprite_row(rb_gfx *g, const rb_sprite *s, int row, int x, int y)
{
    if (!s || row < 0 || row >= s->h) return;
    const uint32_t *p = s->argb + (size_t)row * (size_t)s->w;
    for (int i = 0; i < s->w; ++i)
        if (p[i] >> 24) g_blend(g, x + i, y, p[i]);
}

/* ---- Windows ------------------------------------------------------------- */

rb_rect pt_window_content(rb_rect f, int titled)
{
    /* 9.2.1: a movable modal dialog, whose 21-row title bar, black line and raised
     * bevel put the face 24 rows down and 4 up from the bottom. */
    if (titled && g_v9) return rr(f.x + 6, f.y + 24, f.w - 12, f.h - 28);
    if (titled) return rr(f.x + 6, f.y + PT_TITLE_H + 1, f.w - 12, f.h - PT_TITLE_H - 7);
    return rr(f.x + 6, f.y + 6, f.w - 12, f.h - 12);
}

int pt_window_extra_h(int titled) { return titled ? (g_v9 ? 28 : PT_TITLE_H + 7) : 12; }

/* Mac OS 9.2.1: titled panels are movable modal dialogs (Apple System Profiler's New
 * Report), untitled ones alerts; both frames include the shadow (w + 1, h + 1). */
static void window9(rb_gfx *g, rb_rect f, const char *title, int titled, const rbf_font *font)
{
    rb_rect box = rr(f.x, f.y, f.w + 1, f.h + 1);
    if (!titled) {
        nine(g, "alert", box);
        return;
    }
    nine(g, "movable", box);
    /* Six pinstripes: white lines, each with a #777 line one pixel right and below. */
    for (int i = 0; i < 6; ++i) {
        g_hline(g, f.x + 5, f.y + 4 + 2 * i, f.w - 11, C_WHITE);
        g_hline(g, f.x + 6, f.y + 5 + 2 * i, f.w - 11, 0x777777u);
    }
    if (!title || !title[0] || !font) return;
    size_t len = strlen(title);
    int ell, n = tx_fit_len(font, title, len, f.w - 60, &ell);
    int tw = tx_width(font, 1, title, (size_t)n) + (ell ? rbf_advance(font, 0x2026) : 0);
    /* Centred with integer maths; the stripes stop 4 (white) and 3 (gray) pixels short. */
    int pen = f.x + (f.w - tw) / 2;
    for (int i = 0; i < 6; ++i) {
        g_hline(g, pen - 4, f.y + 4 + 2 * i, tw + 6, 0xCCCCCCu);
        g_hline(g, pen - 3, f.y + 5 + 2 * i, tw + 6, 0xCCCCCCu);
    }
    tx_draw_fit(g, font, pen, f.y + 15, f.w - 60, title, C_BLACK);
}

static void pinstripes(rb_gfx *g, int x0, int x1, int y)
{
    /* Six raised lines: each white line sits one pixel left of its shadow. */
    if (x1 - x0 < 2) return;
    for (int i = 0; i < 6; ++i) {
        g_hline(g, x0 - 1, y + 2 * i, x1 - x0, C_WHITE);
        g_hline(g, x0, y + 2 * i + 1, x1 - x0, C_94);
    }
}

void pt_window(rb_gfx *g, rb_rect f, const char *title, int titled, const rbf_font *font)
{
    if (g_v9) {
        window9(g, f, title, titled, font);
        return;
    }
    const int x0 = f.x, y0 = f.y, x1 = f.x + f.w - 1, y1 = f.y + f.h - 1;
    /* Outline plus a one-pixel drop shadow on the right and bottom. */
    g_frame(g, f, C_BLACK);
    g_vline(g, x1 + 1, y0 + 1, f.h, C_BLACK);
    g_hline(g, x0 + 1, y1 + 1, f.w, C_BLACK);
    /* Raised outer bevel: white top/left, B5 bottom/right, face between. */
    g_fill(g, x0 + 1, y0 + 1, f.w - 2, f.h - 2, C_DE);
    g_hline(g, x0 + 1, y0 + 1, f.w - 2, C_WHITE);
    g_vline(g, x0 + 1, y0 + 1, f.h - 2, C_WHITE);
    g_hline(g, x0 + 1, y1 - 1, f.w - 2, C_B5);
    g_vline(g, x1 - 1, y0 + 1, f.h - 2, C_B5);
    g_pixel(g, x1 - 1, y0 + 1, C_DE);
    g_pixel(g, x0 + 1, y1 - 1, C_DE);
    /* Inset content: B5 above/left, white below/right, then a black line. */
    rb_rect c = pt_window_content(f, titled);
    g_hline(g, c.x - 2, c.y - 2, c.w + 3, C_B5);
    g_vline(g, c.x - 2, c.y - 2, c.h + 3, C_B5);
    g_hline(g, c.x - 1, c.y + c.h + 1, c.w + 3, C_WHITE);
    g_vline(g, c.x + c.w + 1, c.y - 1, c.h + 3, C_WHITE);
    g_frame(g, r_out(c, 1), C_BLACK);
    g_fillr(g, c, C_E6);
    if (!titled) return;

    /* Title bar: pinstripes from y0+3 to y0+14, broken around the title. */
    const int stripe_l = x0 + 9, stripe_r = x1 - 8;
    if (title && title[0] && font) {
        size_t len = strlen(title);
        int ell, n = tx_fit_len(font, title, len, f.w - 60, &ell);
        int tw = tx_width(font, 1, title, (size_t)n) + (ell ? rbf_advance(font, 0x2026) : 0);
        int tx = x0 + (f.w - tw) / 2;
        pinstripes(g, stripe_l, tx - 5, y0 + 3);
        pinstripes(g, tx + tw + 5, stripe_r, y0 + 3);
        tx_draw_fit(g, font, tx, y0 + 14, f.w - 60, title, C_BLACK);
    } else {
        pinstripes(g, stripe_l, stripe_r, y0 + 3);
    }
}

/* ---- Push buttons (edge rows measured from a 20-pixel Platinum button) ---- */

typedef struct { const char *left; char fill; const char *right; } edge_row;

static const edge_row kButtonTop[4] = {
    {"  3K", 'K', "K3  "},
    {" KcE", 'E', "EcK "},
    {"3cWW", 'W', "WEc3"},
    {"KEWW", 'E', "EC9K"},
};
static const edge_row kButtonMiddle = {"KEWE", 'E', "EC9K"};
static const edge_row kButtonBottom[4] = {
    {"KEWE", 'E', "CC9K"},
    {"3cEC", 'C', "C993"},
    {" Kc9", '9', "99K "},
    {"  3K", 'K', "K3  "},
};
/* The default-button ring, 3 pixels outside the button on every side. */
static const edge_row kRingTop[5] = {
    {"   3", 'K', "3   "},
    {"  KE", 'E', "DK  "},
    {" KEE", 'C', "CcK "},
    {"3EEC9", ' ', "9CCA3"},
    {"KEC9", ' ', "9C9K"},
};
static const edge_row kRingMiddle = {"KEC", ' ', "C9K"};
static const edge_row kRingBottom[5] = {
    {"KEC9", ' ', "9C9K"},
    {"3DCC9", ' ', "9CA93"},
    {" KcC", 'C', "A9K "},
    {"  KA", '9', "9K  "},
    {"   3", 'K', "3   "},
};

static uint32_t map_state(uint32_t c, int state)
{
    if (state == PT_PRESSED) {
        switch (c) {
        case C_WHITE: return C_3A;
        case C_E6: return C_73;
        case C_CE: return C_63;
        case C_C5: return C_94;
        case C_94: return C_A5;
        default: return c;
        }
    }
    if (state == PT_DISABLED) {
        switch (c) {
        case C_BLACK: return C_94;
        case C_3A: return C_B5;
        case C_94: return C_C5;
        default: return c;
        }
    }
    return c;
}

static void edge(rb_gfx *g, int x, int y, int w, const edge_row *row, int state)
{
    int nl = (int)strlen(row->left), nr = (int)strlen(row->right), ok;
    for (int i = 0; i < nl; ++i) {
        uint32_t c = code_color(row->left[i], &ok);
        if (ok) g_pixel(g, x + i, y, map_state(c, state));
    }
    uint32_t fill = code_color(row->fill, &ok);
    if (ok) g_hline(g, x + nl, y, w - nl - nr, map_state(fill, state));
    for (int i = 0; i < nr; ++i) {
        uint32_t c = code_color(row->right[i], &ok);
        if (ok) g_pixel(g, x + w - nr + i, y, map_state(c, state));
    }
}

static void stretch(rb_gfx *g, rb_rect r, const edge_row *top, int ntop, const edge_row *mid,
                    const edge_row *bottom, int nbottom, int state)
{
    for (int i = 0; i < ntop; ++i) edge(g, r.x, r.y + i, r.w, &top[i], state);
    for (int y = r.y + ntop; y < r.y + r.h - nbottom; ++y) edge(g, r.x, y, r.w, mid, state);
    for (int i = 0; i < nbottom; ++i) edge(g, r.x, r.y + r.h - nbottom + i, r.w, &bottom[i], state);
}

void pt_button(rb_gfx *g, rb_rect r, const char *label, int state, int is_default, const rbf_font *f)
{
    if (g_v9) {
        /* The default sprite is the button inside its ring; a disabled default has no ring. */
        if (is_default && state != PT_DISABLED)
            nine(g, state == PT_PRESSED ? "button-default-pressed" : "button-default", r_out(r, 3));
        else
            nine(g, state == PT_PRESSED ? "button-pressed" : state == PT_DISABLED ? "button-disabled" : "button-normal", r);
    } else {
        if (is_default)
            stretch(g, r_out(r, 3), kRingTop, 5, &kRingMiddle, kRingBottom, 5,
                    state == PT_DISABLED ? PT_DISABLED : PT_NORMAL);
        stretch(g, r, kButtonTop, 4, &kButtonMiddle, kButtonBottom, 4, state);
    }
    if (!label || !f) return;
    uint32_t fg = state == PT_PRESSED ? C_WHITE : state == PT_DISABLED ? g_pal.disabled : C_BLACK;
    size_t len = strlen(label);
    int ell, n = tx_fit_len(f, label, len, r.w - 8, &ell);
    int tw = tx_width(f, 1, label, (size_t)n) + (ell ? rbf_advance(f, 0x2026) : 0);
    int baseline = r.y + (r.h - f->cap) / 2 + f->cap;
    tx_draw_fit(g, f, r.x + (r.w - tw) / 2, baseline, r.w - 8, label, fg);
}

/* ---- Checkboxes and radio buttons (measured 12x12 Platinum controls) ----- */

static const char *const kCheckOff[12] = {
    "KKKKKKKKKKKK",
    "KWWWWWWWWWEK",
    "KWEEEEEEEEAK",
    "KWEEEEEEEEAK",
    "KWEEEEEEEEAK",
    "KWEEEEEEEEAK",
    "KWEEEEEEEEAK",
    "KWEEEEEEEEAK",
    "KWEEEEEEEEAK",
    "KWEEEEEEEEAK",
    "KEAAAAAAAAAK",
    "KKKKKKKKKKKK",
};
static const char *const kCheckOn[12] = {
    "KKKKKKKKKKKK  ",
    "KWWWWWWWWWEKK ",
    "KWEEEEEEEEKK9C",
    "KWEEEEEEEKKKC ",
    "KWEEEEEEKK7K  ",
    "KWKKEEEKK99K  ",
    "KWEKKEKK9CAK  ",
    "KWECKKK9CEAK  ",
    "KWEECK9CEEAK  ",
    "KWEEE9CEEEAK  ",
    "KEAAAAAAAAAK  ",
    "KKKKKKKKKKKK  ",
};
static const char *const kRadioOff[12] = {
    "    6KK6    ",
    "  K7EEEc6K  ",
    " KcEFWWWEAK ",
    " 7EFWWFFEc6 ",
    "6EFWWFFEEcA6",
    "KEWWFFEEccAK",
    "KEWFFEEccCAK",
    "6cWFEEccCCA6",
    " 7EEEccCCA6 ",
    " KAcccCCAAK ",
    "  K7AAAA6K  ",
    "    6KK6    ",
};
static const char *const kRadioOn[12] = {
    "    3KK3    ",
    "  3K67776K  ",
    " 367999AABK ",
    " K79KKKKBB6 ",
    "369KKKKKKBc6",
    "K79KKKKKKccK",
    "K79KKKKKKcEK",
    "37AKKKKKKEW6",
    " 3ABKKKKEW6 ",
    " 3BBBccEWWK ",
    "  36ccEW6K  ",
    "    6KK6    ",
};

static void control_sprite(rb_gfx *g, int x, int y, const char *const *rows, int state)
{
    for (int r = 0; r < 12; ++r)
        for (int c = 0; rows[r][c]; ++c) {
            int ok;
            uint32_t col = code_color(rows[r][c], &ok);
            if (!ok) continue;
            if (state == PT_PRESSED) {
                /* A pressed control darkens its face while tracking. */
                if (col == C_WHITE || col == C_F7) col = C_94;
                else if (col == C_E6 || col == C_CE) col = C_73;
                else if (col == C_C5 || col == C_A5) col = C_63;
            } else if (state == PT_DISABLED) {
                if (col == C_BLACK || col == C_3A) col = C_94;
                else if (col == C_63 || col == C_73) col = C_A5;
                else if (col == C_94 || col == C_A5) col = C_C5;
            }
            g_pixel(g, x + c, y + r, col);
        }
}

static const char *const kStateSuffix[] = {"", "-pressed", "-disabled"};

void pt_checkbox(rb_gfx *g, int x, int y, int checked, int state)
{
    if (g_v9) {
        char name[32];
        snprintf(name, sizeof(name), "checkbox-%s%s", checked ? "on" : "off", kStateSuffix[state]);
        blit(g, name, x, y);   /* 14 x 12: the check overhangs the 12-pixel box */
        return;
    }
    control_sprite(g, x, y, checked ? kCheckOn : kCheckOff, state);
}

void pt_radio(rb_gfx *g, int x, int y, int on, int state)
{
    if (g_v9) {
        char name[32];
        snprintf(name, sizeof(name), "radio-%s%s", on ? "on" : "off", kStateSuffix[state]);
        blit(g, name, x, y);
        return;
    }
    control_sprite(g, x, y, on ? kRadioOn : kRadioOff, state);
}

/* Box left edge to label: 9.2.1 draws the text 16 pixels from the box (web: 14 + 2). */
int pt_check_text_dx(void) { return g_v9 ? 16 : 18; }

/* Edit text starts 4 pixels inside the field's black frame in 9.2.1 (web input padding 3 + frame 1). */
int pt_field_text_dx(void) { return g_v9 ? 4 : 5; }

/* ---- Group boxes, separators, sunken frames ------------------------------ */

void pt_group(rb_gfx *g, rb_rect r, const char *label, const rbf_font *f, int disabled)
{
    if (g_v9) {
        /* The title's baseline is one row below the frame's top line; the title starts
         * 12 pixels in and the lines stop 3 pixels before and after it. */
        int top = label && label[0] ? r.y + 11 : r.y;
        nine(g, "group", rr(r.x, top, r.w, r.y + r.h - top));
        if (label && label[0] && f) {
            int tw = tx_width(f, 1, label, strlen(label));
            if (tw > r.w - 24) tw = r.w - 24;
            g_fill(g, r.x + 9, top, tw + 6, 2, g_pal.face);
            tx_draw_fit(g, f, r.x + 12, top + 1, r.w - 24, label, disabled ? g_pal.disabled : C_BLACK);
        }
        return;
    }
    /* Etched frame: a dark line with a white line just inside/below it. The
     * frame's top edge runs through the middle of the label's capitals. */
    int top = label && label[0] ? r.y + 7 : r.y;
    rb_rect outer = rr(r.x, top, r.w - 1, r.y + r.h - top - 1);
    g_frame(g, rr(outer.x + 1, outer.y + 1, outer.w, outer.h), C_WHITE);
    g_frame(g, outer, C_94);
    if (label && label[0] && f) {
        int tw = tx_width(f, 1, label, strlen(label));
        if (tw > r.w - 24) tw = r.w - 24;
        g_fill(g, r.x + 8, r.y, tw + 6, f->cap + 5, C_E6);
        tx_draw_fit(g, f, r.x + 11, r.y + 2 + f->cap, r.w - 24, label, disabled ? C_DISABLED : C_BLACK);
    }
}

void pt_separator(rb_gfx *g, int x, int y, int w)
{
    if (g_v9) {
        g_hline(g, x, y, w, 0x888888u);
        g_hline(g, x + 1, y + 1, w, C_WHITE);
        return;
    }
    g_hline(g, x, y, w, C_94);
    g_hline(g, x + 1, y + 1, w, C_WHITE);
}

void pt_sunken(rb_gfx *g, rb_rect r, uint32_t fill)
{
    /* Gray above/left, white below/right, around a black frame. */
    g_hline(g, r.x - 1, r.y - 1, r.w + 1, C_94);
    g_vline(g, r.x - 1, r.y - 1, r.h + 1, C_94);
    g_hline(g, r.x - 1, r.y + r.h, r.w + 2, C_WHITE);
    g_vline(g, r.x + r.w, r.y - 1, r.h + 2, C_WHITE);
    g_frame(g, r, C_BLACK);
    g_fill(g, r.x + 1, r.y + 1, r.w - 2, r.h - 2, fill);
}

/* Edit fields and lists: the 9.2.1 frame is a gray etch around a black line; focus
 * replaces the etch with a two-pixel variation-colour ring. */
static void framed9(rb_gfx *g, rb_rect c, uint32_t fill, int focused)
{
    g_fill(g, c.x + 1, c.y + 1, c.w - 2, c.h - 2, fill);
    if (focused) nine(g, "field-focus", r_out(c, 2));
    else nine(g, "field", r_out(c, 1));
}

void pt_field(rb_gfx *g, rb_rect c, int disabled, int focused)
{
    if (g_v9) {
        framed9(g, c, disabled ? g_pal.field_off : g_pal.field, focused && !disabled);
        return;
    }
    pt_sunken(g, c, disabled ? C_F7 : C_WHITE);
    if (disabled) g_frame(g, c, C_A5);
    g_hline(g, c.x + 1, c.y + 1, c.w - 2, C_E6);
    g_vline(g, c.x + 1, c.y + 1, c.h - 2, C_E6);
}

void pt_listbox(rb_gfx *g, rb_rect r, int disabled, int focused)
{
    if (g_v9) {
        framed9(g, r, disabled ? g_pal.field_off : g_pal.field, focused && !disabled);
        return;
    }
    pt_sunken(g, r, disabled ? C_F7 : C_WHITE);
    if (disabled) g_frame(g, r, C_A5);
}

/* ---- Scroll bar ------------------------------------------------------------ */

static const char *const kScrollUp[16] = {
    "KKKKKKKKKKKKKKKK",
    "KWWWWWWWWWWWWWEK",
    "KWEEEEEEEEEEEEcK",
    "KWEEEEEEEEEEEEcK",
    "KWEEEEEEEEEEEEcK",
    "KWEEEEEEEEEEEEcK",
    "KWEEEEEKKEEEEEcK",
    "KWEEEEKKKKEEEEcK",
    "KWEEEKKKKKKEEEcK",
    "KWEEKKKKKKKKEEcK",
    "KWEEEEEEEEEEEEcK",
    "KWEEEEEEEEEEEEcK",
    "KWEEEEEEEEEEEEcK",
    "KWEEEEEEEEEEEEcK",
    "KEcccccccccccccK",
    "KKKKKKKKKKKKKKKK",
};
static const char *const kThumb[17] = {
    "KKKKKKKKKKKKKKKK",
    "KFllllllllllllaK",
    "KlaaaaaaaaaaaasK",
    "KlaaaaaaaaaaaasK",
    "KlaaFllllllaaasK",
    "KlaaadddddddaasK",
    "KlaaFllllllaaasK",
    "KlaaadddddddaasK",
    "KlaaFllllllaaasK",
    "KlaaadddddddaasK",
    "KlaaFllllllaaasK",
    "KlaaadddddddaasK",
    "KlaaaaaaaaaaaasK",
    "KlaaaaaaaaaaaasK",
    "KlaaaaaaaaaaaasK",
    "KasssssssssssssK",
    "KKKKKKKKKKKKKKKK",
};

static void arrow_box(rb_gfx *g, int x, int y, int up, int active, int pressed)
{
    for (int row = 0; row < 16; ++row)
        for (int col = 0; col < 16; ++col) {
            /* The down box is the up box with its arrow flipped. */
            int src = (!up && row >= 6 && row <= 9) ? 15 - row : row;
            int ok;
            uint32_t c = code_color(kScrollUp[src][col], &ok);
            if (row >= 6 && row <= 9 && c == C_BLACK && col > 1 && col < 14) {
                if (!active) c = C_A5;
                else if (pressed) c = C_WHITE;
            } else if (pressed && c != C_BLACK) {
                c = c == C_WHITE ? C_63 : C_73;
            } else if (!active && c != C_BLACK) {
                c = C_F7;
            } else if (!active && col > 0 && col < 15 && row == (up ? 15 : 0)) {
                c = C_73;   /* inactive boxes are divided from the track in gray */
            }
            g_pixel(g, x + col, y + row, c);
        }
}

int pt_thumb_pos(rb_rect r, int total, int visible, int first)
{
    int track_y, track_h;
    pt_bar_track(r, &track_y, &track_h);
    int span = track_h - PT_THUMB_H;
    int max_first = total - visible;
    if (max_first <= 0 || span <= 0) return track_y;
    if (first < 0) first = 0;
    if (first > max_first) first = max_first;
    return track_y + (int)(((long long)first * span + max_first / 2) / max_first);
}

/* A 9.2.1 arrow box: the bundle's arrow sprite, a black line above 14 rows of box.
 * Pressed and inactive boxes are designed, not measured. */
static void arrow_box9(rb_gfx *g, int x, int y, int up, int active, int pressed)
{
    const rb_sprite *s = assets_sprite(up ? "scroll-up" : "scroll-down");
    if (!s) return;
    for (int row = 0; row < 15 && row < s->h; ++row)
        for (int col = 0; col < 15 && col < s->w; ++col) {
            uint32_t c = s->argb[row * s->w + col];
            if (!(c >> 24)) continue;
            c &= 0xFFFFFFu;
            if (col > 0 && pressed) {
                c = c == C_WHITE ? 0x999999u : c == C_BLACK ? C_WHITE : c == 0xBBBBBBu ? 0x555555u : 0x777777u;
            } else if (col > 0 && !active && c == C_BLACK) {
                c = 0x999999u;
            }
            g_pixel(g, x + col, y + row, c);
        }
}

/* Scroll bar geometry. 8.1: an arrow at each end. 9.2.1 (as the reference install is set
 * up): both arrows together at the bottom, the track from the top. */
void pt_bar_track(rb_rect bar, int *y, int *h)
{
    *y = g_v9 ? bar.y + 1 : bar.y + 16;
    *h = bar.h - 32;
}

int pt_bar_arrow(rb_rect bar, int y)
{
    if (y >= bar.y + bar.h - 16) return SB_DOWN;
    if (g_v9) return y >= bar.y + bar.h - 31 ? SB_UP : SB_NONE;
    return y < bar.y + 16 ? SB_UP : SB_NONE;
}

/* 9.2.1 scroll bar: column 0 is the bar's own black line, 1-14 the track, 15 the frame it
 * sits against (a list's own line); the arrow boxes sit together at the bottom. */
static void scrollbar9(rb_gfx *g, rb_rect r, int total, int visible, int first, int pressed, int thumb_y)
{
    int active = total > visible && visible > 0;
    int x = r.x, track_y, track_h;
    pt_bar_track(r, &track_y, &track_h);
    g_vline(g, x + 15, r.y, r.h, C_BLACK);
    g_hline(g, x, r.y, 16, C_BLACK);
    arrow_box9(g, x, r.y + r.h - 31, 1, active, pressed == SB_UP);
    arrow_box9(g, x, r.y + r.h - 16, 0, active, pressed == SB_DOWN);
    g_hline(g, x, r.y + r.h - 1, 16, C_BLACK);
    if (track_h <= 0) return;
    const rb_sprite *track = assets_sprite("scroll-track-v"), *thumb = assets_sprite("scroll-thumb-v");
    for (int i = 0; i < track_h; ++i) sprite_row(g, track, i < 2 ? i : 2, x, track_y + i);
    if (!active || track_h < PT_THUMB_H) return;
    int ty = thumb_y >= 0 ? thumb_y : pt_thumb_pos(r, total, visible, first);
    if (ty < track_y) ty = track_y;
    if (ty > track_y + track_h - PT_THUMB_H) ty = track_y + track_h - PT_THUMB_H;
    /* Thumb rows: black, light edge, 13 face rows (grip in rows 4-11), dark edge, black;
     * then the track's two shadow rows below it. */
    sprite_row(g, thumb, 0, x, ty);
    sprite_row(g, thumb, 1, x, ty + 1);
    for (int i = 2; i < 15; ++i) sprite_row(g, thumb, 2, x, ty + i);
    sprite_row(g, thumb, 3, x, ty + 15);
    sprite_row(g, thumb, 4, x, ty + 16);
    for (int i = 0; i < 2 && ty + 17 + i < track_y + track_h; ++i) sprite_row(g, thumb, 5 + i, x, ty + 17 + i);
    blit(g, "scroll-grip-v", x, ty + 4);
}

void pt_scrollbar(rb_gfx *g, rb_rect r, int total, int visible, int first, int pressed, int thumb_y)
{
    if (g_v9) {
        scrollbar9(g, r, total, visible, first, pressed, thumb_y);
        return;
    }
    int active = total > visible && visible > 0;
    arrow_box(g, r.x, r.y, 1, active, pressed == SB_UP);
    arrow_box(g, r.x, r.y + r.h - 16, 0, active, pressed == SB_DOWN);
    int track_y = r.y + 16, track_h = r.h - 32;
    if (track_h <= 0) return;
    g_vline(g, r.x, track_y, track_h, C_BLACK);
    g_vline(g, r.x + 15, track_y, track_h, C_BLACK);
    if (!active) {
        g_fill(g, r.x + 1, track_y, 14, track_h, C_F7);
        return;
    }
    /* Sunken channel, then the lavender thumb with its grip ridges. */
    for (int y = track_y; y < track_y + track_h; ++y) {
        g_pixel(g, r.x + 1, y, C_94);
        g_pixel(g, r.x + 2, y, C_A5);
        g_hline(g, r.x + 3, y, 10, C_C5);
        g_pixel(g, r.x + 13, y, C_CE);
        g_pixel(g, r.x + 14, y, C_DE);
    }
    g_hline(g, r.x + 1, track_y, 13, C_94);
    g_hline(g, r.x + 2, track_y + 1, 11, C_A5);
    if (track_h < PT_THUMB_H) return;
    int ty = thumb_y >= 0 ? thumb_y : pt_thumb_pos(r, total, visible, first);
    if (ty < track_y) ty = track_y;
    if (ty > track_y + track_h - PT_THUMB_H) ty = track_y + track_h - PT_THUMB_H;
    g_coded(g, r.x, ty, kThumb, 17, 0);
}

/* ---- Popup button ----------------------------------------------------------- */

void pt_arrow(rb_gfx *g, int cx, int y, int down, uint32_t c)
{
    for (int i = 0; i < 4; ++i) {
        int row = down ? i : 3 - i;
        g_hline(g, cx - 3 + i, y + row, 7 - 2 * i, c);
    }
}

void pt_popup(rb_gfx *g, rb_rect r, const char *text, int state, const rbf_font *f)
{
    if (g_v9) {
        /* The open (pressed) pop-up keeps its normal face; its menu shows the state. */
        nine(g, state == PT_DISABLED ? "popup-disabled" : "popup", r);
        if (text && f) {
            int baseline = r.y + (r.h - f->cap) / 2 + f->cap;
            tx_draw_fit(g, f, r.x + 6, baseline, r.w - 6 - 26, text, state == PT_DISABLED ? g_pal.disabled : C_BLACK);
        }
        return;
    }
    /* A push-button body with an arrow segment on the right. */
    stretch(g, r, kButtonTop, 4, &kButtonMiddle, kButtonBottom, 4, state);
    int ax = r.x + r.w - 19;
    uint32_t div_dark = state == PT_PRESSED ? C_3A : state == PT_DISABLED ? C_C5 : C_94;
    uint32_t div_light = state == PT_PRESSED ? C_63 : C_WHITE;
    g_vline(g, ax, r.y + 3, r.h - 6, div_dark);
    g_vline(g, ax + 1, r.y + 3, r.h - 6, div_light);
    uint32_t fg = state == PT_PRESSED ? C_WHITE : state == PT_DISABLED ? C_94 : C_BLACK;
    pt_arrow(g, ax + 10, r.y + (r.h - 4) / 2, 1, fg);
    if (text && f) {
        int baseline = r.y + (r.h - f->cap) / 2 + f->cap;
        tx_draw_fit(g, f, r.x + 9, baseline, ax - r.x - 13, text, fg);
    }
}

/* ---- Progress bar ----------------------------------------------------------- */

static uint32_t lerp(uint32_t a, uint32_t b, int t, int n)
{
    if (n <= 0) return a;
    uint32_t out = 0;
    for (int sh = 0; sh <= 16; sh += 8) {
        int ca = (int)((a >> sh) & 255u), cb = (int)((b >> sh) & 255u);
        int v = ca + (cb - ca) * t / n;
        out |= (uint32_t)v << sh;
    }
    return out;
}

static uint32_t gray_of(uint32_t c)
{
    unsigned r = (c >> 16) & 255u, gg = (c >> 8) & 255u, b = c & 255u;
    unsigned v = (r * 30 + gg * 59 + b * 11) / 100;
    v = (v + 0xE6) / 2;
    return (v << 16) | (v << 8) | v;
}

/* 9.2.1 progress bar: etch, black frame and track from the bundle; the fill keeps its
 * rounded end and the track's shadow after it (Mac OS: fill = round(interior * v), at
 * least 5). The indeterminate stripes are designed, not measured. */
static void progress9(rb_gfx *g, rb_rect r, double v, int indeterminate, int phase)
{
    rb_rect box = r_out(r, 1);
    nine(g, "progress", box);
    int inner = box.w - 4;
    rb_rect in = rr(box.x + 2, box.y + 2, inner, box.h - 4);
    if (inner <= 0 || in.h <= 0) return;
    rb_rect old = g_push_clip(g, in);
    if (indeterminate) {
        for (int y = 0; y < in.h; ++y)
            for (int x = 0; x < inner; ++x)
                g_pixel(g, in.x + x, in.y + y, (((x + y - phase) % 8 + 8) % 8) < 4 ? g_ramp[2] : g_ramp[4]);
    } else if (v > 0) {
        if (v > 1) v = 1;
        int n = (int)(inner * v + 0.5);
        if (n < 5) n = 5;
        /* The fill's leading edge throws a 3 px shadow onto the track; a full bar ends in the
         * edge itself, its shadow cut off by the frame (About This Computer, 9.2.2). */
        if (n > inner) n = inner;
        nine_s(g, assets_sprite("progress-fill"), rr(in.x, in.y, n + 3, in.h), 0, 6, 0, 2);
    }
    g->clip = old;
}

void pt_progress(rb_gfx *g, rb_rect r, double v, int indeterminate, int phase, int disabled)
{
    if (g_v9) {
        progress9(g, r, v, indeterminate, phase);
        return;
    }
    /* Sunken bevel, black frame, then a lavender bar shaded top to bottom
     * over a recessed light track. Indeterminate bars show moving diagonal
     * stripes ("barber pole"). */
    g_hline(g, r.x - 1, r.y - 1, r.w + 1, C_94);
    g_vline(g, r.x - 1, r.y - 1, r.h + 1, C_94);
    g_hline(g, r.x - 1, r.y + r.h, r.w + 2, C_WHITE);
    g_vline(g, r.x + r.w, r.y - 1, r.h + 2, C_WHITE);
    g_frame(g, r, C_BLACK);
    rb_rect in = rr(r.x + 1, r.y + 1, r.w - 2, r.h - 2);
    if (r_empty(in)) return;
    int n = in.h - 1;
    if (indeterminate) {
        for (int y = 0; y < in.h; ++y) {
            uint32_t dark = lerp(0x8C8AEFu, 0x4A4DA5u, y, n), light = lerp(0xF2F1FFu, 0xB5B2FFu, y, n);
            if (y == 0) { dark = 0xB5B2FFu; light = C_WHITE; }
            for (int x = 0; x < in.w; ++x) {
                int s = ((x - y + phase) % 16 + 16) % 16;
                uint32_t c = s < 8 ? dark : light;
                g_pixel(g, in.x + x, in.y + y, disabled ? gray_of(c) : c);
            }
        }
        return;
    }
    if (v < 0) v = 0;
    if (v > 1) v = 1;
    int fw = (int)(v * in.w + 0.5);
    for (int y = 0; y < in.h; ++y) {
        uint32_t bar = y == 0 ? 0xC6C4FFu : lerp(0xA5A3F7u, 0x4A4DA5u, y - 1, n - 1);
        uint32_t track = y == 0 ? C_A5 : lerp(C_CE, C_F7, y - 1, n - 1);
        if (disabled) bar = gray_of(bar);
        g_hline(g, in.x, in.y + y, fw, bar);
        g_hline(g, in.x + fw, in.y + y, in.w - fw, track);
    }
    if (fw > 0 && fw < in.w) g_vline(g, in.x + fw - 1, in.y, in.h, disabled ? C_73 : 0x3A3D8Cu);
}

/* ---- Bevel buttons and sliders (Monitors and Sound, Mac OS 9.2.2) -------------- */

/* A bevel button's rows and columns by edge class: 0-3 from the near edge, 4 the middle,
 * 5-8 the far edge's last four, measured on Monitors' 40 x 40 buttons.  0 is transparent
 * (the rounded corners), else 0xRRGGBB | 0x1000000. */
#define BV(c) (0x1000000u | (c))
static const uint32_t kBevelUp[9][9] = {
    {0, 0, BV(0x222222), BV(0), BV(0), BV(0), BV(0x222222), 0, 0},
    {0, BV(0), BV(0xBBBBBB), BV(0xDDDDDD), BV(0xDDDDDD), BV(0xDDDDDD), BV(0xBBBBBB), BV(0), 0},
    {BV(0x222222), BV(0xBBBBBB), BV(0xFFFFFF), BV(0xFFFFFF), BV(0xFFFFFF), BV(0xFFFFFF), BV(0xDDDDDD), BV(0xBBBBBB), BV(0x222222)},
    {BV(0), BV(0xDDDDDD), BV(0xFFFFFF), BV(0xFFFFFF), BV(0xDDDDDD), BV(0xDDDDDD), BV(0xAAAAAA), BV(0x777777), BV(0)},
    {BV(0), BV(0xDDDDDD), BV(0xFFFFFF), BV(0xDDDDDD), BV(0xDDDDDD), BV(0xDDDDDD), BV(0xAAAAAA), BV(0x777777), BV(0)},
    {BV(0), BV(0xDDDDDD), BV(0xFFFFFF), BV(0xDDDDDD), BV(0xDDDDDD), BV(0xAAAAAA), BV(0xAAAAAA), BV(0x777777), BV(0)},
    {BV(0x222222), BV(0xBBBBBB), BV(0xDDDDDD), BV(0xAAAAAA), BV(0xAAAAAA), BV(0xAAAAAA), BV(0x777777), BV(0x777777), BV(0x222222)},
    {0, BV(0), BV(0xBBBBBB), BV(0x777777), BV(0x777777), BV(0x777777), BV(0x777777), BV(0), 0},
    {0, 0, BV(0x222222), BV(0), BV(0), BV(0), BV(0x222222), 0, 0},
};
static const uint32_t kBevelDown[9][9] = {
    {0, 0, BV(0x222222), BV(0), BV(0), BV(0), BV(0x222222), 0, 0},
    {0, BV(0), BV(0x444444), BV(0x444444), BV(0x444444), BV(0x444444), BV(0x444444), BV(0), 0},
    {BV(0x222222), BV(0x444444), BV(0x444444), BV(0x555555), BV(0x555555), BV(0x555555), BV(0x666666), BV(0x777777), BV(0x222222)},
    {BV(0), BV(0x444444), BV(0x555555), BV(0x555555), BV(0x666666), BV(0x666666), BV(0x777777), BV(0x888888), BV(0)},
    {BV(0), BV(0x444444), BV(0x555555), BV(0x666666), BV(0x666666), BV(0x666666), BV(0x777777), BV(0x888888), BV(0)},
    {BV(0), BV(0x444444), BV(0x555555), BV(0x666666), BV(0x666666), BV(0x777777), BV(0x777777), BV(0x888888), BV(0)},
    {BV(0x222222), BV(0x444444), BV(0x666666), BV(0x777777), BV(0x777777), BV(0x777777), BV(0x888888), BV(0x888888), BV(0x222222)},
    {0, BV(0), BV(0x777777), BV(0x888888), BV(0x888888), BV(0x888888), BV(0x888888), BV(0), 0},
    {0, 0, BV(0x222222), BV(0), BV(0), BV(0), BV(0x222222), 0, 0},
};
#undef BV

static int bevel_class(int i, int n)
{
    if (i < 4) return i;
    if (i >= n - 4) return 8 - (n - 1 - i);
    return 4;
}

void pt_bevel(rb_gfx *g, rb_rect r, int pressed)
{
    const uint32_t (*t)[9] = pressed ? kBevelDown : kBevelUp;
    for (int y = 0; y < r.h; ++y)
        for (int x = 0; x < r.w; ++x) {
            uint32_t c = t[bevel_class(y, r.h)][bevel_class(x, r.w)];
            if (c) g_pixel(g, r.x + x, r.y + y, c & 0xFFFFFFu);
        }
}

/* Sound's track (the classic panel draws it from the same rows as the bundle's sprite):
 * 2 columns, the middle repeated, 3 columns; '.' transparent. */
static const char *const kSliderTrack[7][3] = {
    {".a", "a", "a.."}, {"aa", "2", "2a."}, {"a2", "a", "a2W"}, {"a2", "a", "a2W"},
    {"a2", "a", "a2W"}, {".W", "2", "2WW"}, {"..", "W", "WW."},
};
/* The plain thumb: K black, A-G the Appearance ramp (pressed: each one step darker). */
static const char *const kSliderThumb[16] = {
    ".KKKKKKKKKKK.", "KABBBBBBBBBCK", "KBCCCCCCCCCDK", "KBCCCCCCCCCDK", "KBCACACACCCDK", "KBCBEBEBECCDK",
    "KBCBEBEBECCDK", "KBCBEBEBECCDK", "KBCBEBEBECCDK", "KBCBEBEBECCDK", "KBCBEBEBECCDK", "KBCCECECECCDK",
    "KBCCCCCCCCCDK", "KBCCCCCCCCCDK", "KCDDDDDDDDDDK", ".KKKKKKKKKKK.",
};

static uint32_t track_color(char c)
{
    return c == 'a' ? 0xAAAAAAu : c == '2' ? 0x222222u : 0xFFFFFFu;
}

void pt_slider(rb_gfx *g, rb_rect t, int thumb_x, int thumb_y, int pressed, int disabled)
{
    /* Without the bundle, Mac OS's default variation (Lavender). */
    static const uint32_t kLavender[8] = {0xEEEEEEu, 0xCCCCFFu, 0x9999FFu, 0x6666CCu,
                                          0x333399u, 0x000088u, 0x000055u, 0x000000u};
    const uint32_t *ramp = g_ramp[0] ? g_ramp : kLavender;
    for (int y = 0; y < 7; ++y)
        for (int x = 0; x < t.w; ++x) {
            char c = x < 2 ? kSliderTrack[y][0][x] : x >= t.w - 3 ? kSliderTrack[y][2][x - (t.w - 3)] : kSliderTrack[y][1][0];
            if (c != '.') g_pixel(g, t.x + x, t.y + y, track_color(c));
        }
    for (int y = 0; y < 16; ++y)
        for (int x = 0; x < SLIDER_THUMB_W; ++x) {
            char c = kSliderThumb[y][x];
            if (c == '.') continue;
            uint32_t v;
            if (c == 'K') {
                v = disabled ? C_A5 : C_BLACK;
            } else {
                int i = c - 'A' + (pressed ? 1 : 0);
                if (c == 'E' && pressed) i = 6;   /* E -> G, as the bundle's pressed thumb */
                v = disabled ? gray_of(ramp[i]) : ramp[i];
            }
            g_pixel(g, thumb_x + x, thumb_y + y, v);
        }
}

/* ---- Focus ring, menus, small marks --------------------------------------- */

void pt_focus_ring(rb_gfx *g, rb_rect r)
{
    if (g_v9) {
        /* Keyboard focus outside fields: the edit-field ring's colour and shape. */
        rb_rect a = r_out(r, 1), b = r_out(r, 2);
        g_frame(g, a, g_ramp[3]);
        g_hline(g, b.x + 1, b.y, b.w - 2, g_ramp[3]);
        g_hline(g, b.x + 1, b.y + b.h - 1, b.w - 2, g_ramp[3]);
        g_vline(g, b.x, b.y + 1, b.h - 2, g_ramp[3]);
        g_vline(g, b.x + b.w - 1, b.y + 1, b.h - 2, g_ramp[3]);
        return;
    }
    /* Two-pixel accent ring just outside r; the outer line's corners are
     * left out so the ring reads as rounded. */
    rb_rect a = r_out(r, 1), b = r_out(r, 2);
    g_frame(g, a, 0x6365CEu);
    g_hline(g, b.x + 1, b.y, b.w - 2, 0x9C9AEFu);
    g_hline(g, b.x + 1, b.y + b.h - 1, b.w - 2, 0x9C9AEFu);
    g_vline(g, b.x, b.y + 1, b.h - 2, 0x9C9AEFu);
    g_vline(g, b.x + b.w - 1, b.y + 1, b.h - 2, 0x9C9AEFu);
}

void pt_menu(rb_gfx *g, rb_rect r)
{
    if (g_v9) {
        nine(g, "menu", rr(r.x, r.y, r.w + 1, r.h + 1));   /* includes the shadow */
        return;
    }
    /* Drop shadow, black outline, raised light face. */
    for (int i = 1; i <= 2; ++i) {
        uint32_t s = i == 1 ? 0x80000000u : 0x40000000u;
        for (int y = r.y + i + 1; y < r.y + r.h + i; ++y) g_blend(g, r.x + r.w - 1 + i, y, s);
        for (int x = r.x + i + 1; x < r.x + r.w + i - 1; ++x) g_blend(g, x, r.y + r.h - 1 + i, s);
    }
    g_frame(g, r, C_BLACK);
    g_fill(g, r.x + 1, r.y + 1, r.w - 2, r.h - 2, C_E6);
    g_hline(g, r.x + 1, r.y + 1, r.w - 2, C_WHITE);
    g_vline(g, r.x + 1, r.y + 1, r.h - 2, C_WHITE);
    g_hline(g, r.x + 2, r.y + r.h - 2, r.w - 3, C_B5);
    g_vline(g, r.x + r.w - 2, r.y + 2, r.h - 3, C_B5);
}

static const char *const kCheck[8] = {
    ".......##",
    "......##.",
    "......##.",
    ".....##..",
    "##..##...",
    ".####....",
    "..##.....",
    "..#......",
};

/* A highlighted menu item: 9.2.1 fills it with the variation colour, a lighter line on top
 * and a darker one below (web .menu .is-hot). */
void pt_menu_row(rb_gfx *g, rb_rect row)
{
    if (!g_v9) {
        g_fillr(g, row, C_MENU_HI);
        return;
    }
    g_fillr(g, row, g_ramp[4]);
    g_hline(g, row.x, row.y, row.w, g_ramp[3]);
    g_hline(g, row.x, row.y + row.h - 1, row.w, g_ramp[5]);
}

/* The mark beside the current item: the system font's check mark in 9.2.1. */
void pt_menu_check(rb_gfx *g, int x, int baseline, uint32_t c, const rbf_font *f)
{
    if (g_v9 && f && rbf_find(f, 0x2713)) {
        tx_draw(g, f, 1, x, baseline, "\xE2\x9C\x93", 3, c);
        return;
    }
    pt_checkmark(g, x + 2, baseline - 8, c);
}

void pt_checkmark(rb_gfx *g, int x, int y, uint32_t c)
{
    for (int r = 0; r < 8; ++r)
        for (int k = 0; kCheck[r][k]; ++k)
            if (kCheck[r][k] == '#') g_pixel(g, x + k, y + r, c);
}

void pt_signal(rb_gfx *g, int x, int y, int bars, int dim)
{
    /* Four rising bars in a 15x12 box (from M5Tab's Wi-Fi list). */
    uint32_t on = dim ? C_94 : C_BLACK, off = C_B5;
    for (int i = 0; i < 4; ++i) {
        int h = 3 * (i + 1);
        rb_rect bar = rr(x + i * 4, y + 12 - h, 3, h);
        if (i < bars) g_fillr(g, bar, on);
        else g_frame(g, bar, off);
    }
}
