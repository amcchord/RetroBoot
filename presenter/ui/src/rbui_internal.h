/* rbui_internal.h - shared definitions for the rbui library */
#ifndef RBUI_INTERNAL_H
#define RBUI_INTERNAL_H

#include "rbui_names.h"
#include <stddef.h>
#include <stdint.h>

#include "rbui.h"
#include "font.h"
#include "icons.h"

typedef struct { int x, y, w, h; } rb_rect;

static inline rb_rect rr(int x, int y, int w, int h) { rb_rect r = {x, y, w, h}; return r; }
static inline int r_empty(rb_rect r) { return r.w <= 0 || r.h <= 0; }
static inline int r_has(rb_rect r, int x, int y) { return x >= r.x && y >= r.y && x < r.x + r.w && y < r.y + r.h; }
static inline rb_rect r_out(rb_rect r, int d) { return rr(r.x - d, r.y - d, r.w + 2 * d, r.h + 2 * d); }
rb_rect r_and(rb_rect a, rb_rect b);
rb_rect r_or(rb_rect a, rb_rect b);   /* bounding box; empty rects ignored */

/* ---- Colours (XRGB) ------------------------------------------------------ */
/* Platinum grays as measured from Mac OS 8.1 in M5Tab-Macintosh's preboot UI
 * (src/basilisk/preboot_ui.cpp, the user's own project), neutralised. */
#define C_BLACK 0x000000u
#define C_WHITE 0xFFFFFFu
#define C_3A 0x3A3A3Au
#define C_63 0x636363u
#define C_73 0x737373u
#define C_94 0x949494u
#define C_A5 0xA5A5A5u
#define C_B5 0xB5B5B5u
#define C_C5 0xC5C5C5u
#define C_CE 0xCECECEu
#define C_DE 0xDEDEDEu   /* window frame, title bar */
#define C_E6 0xE6E6E6u   /* dialog body, button face */
#define C_F7 0xF7F7F7u
#define C_ACC_L 0xDEDBFFu   /* lavender accent ramp (scroll thumb, progress) */
#define C_ACC 0xB5B2FFu
#define C_ACC_S 0x8486DEu
#define C_ACC_D 0x5255B5u
#define C_HILITE 0xCCCCFFu   /* text / list selection */
#define C_MENU_HI 0x6363CEu  /* highlighted menu item */
#define C_TEXT_DIM 0x737373u /* gray detail text */
#define C_DISABLED 0x949494u /* disabled text */

/* Colours that change with the look: the Mac OS 8.1 values above, or Mac OS 9.2.1's
 * when the Platinum bundle supplies its controls (platinum.c pt_theme_update). */
typedef struct {
    uint32_t face;          /* window/dialog body */
    uint32_t text, disabled;
    uint32_t dim, dim_disabled;   /* secondary (detail) text */
    uint32_t placeholder;
    uint32_t hilite, hilite_off;  /* list/text selection; selection in a disabled list */
    uint32_t menu_hi;             /* highlighted menu item */
    uint32_t field, field_off;    /* edit field / list background, disabled */
} rb_pal;
extern rb_pal g_pal;

/* ---- Canvas -------------------------------------------------------------- */
typedef struct {
    uint32_t *px;
    int stride, w, h;
    rb_rect clip;     /* canvas coordinates */
    int dx, dy;       /* translation added to every drawing coordinate */
} rb_gfx;

void g_fill(rb_gfx *g, int x, int y, int w, int h, uint32_t c);
static inline void g_fillr(rb_gfx *g, rb_rect r, uint32_t c) { g_fill(g, r.x, r.y, r.w, r.h, c); }
static inline void g_hline(rb_gfx *g, int x, int y, int w, uint32_t c) { g_fill(g, x, y, w, 1, c); }
static inline void g_vline(rb_gfx *g, int x, int y, int h, uint32_t c) { g_fill(g, x, y, 1, h, c); }
void g_pixel(rb_gfx *g, int x, int y, uint32_t c);
void g_blend(rb_gfx *g, int x, int y, uint32_t argb);
/* Mac OS 9 smoothed text: per channel bg + (((fg - bg) * 17 * level) >> 8); 16 = fg. */
void g_level(rb_gfx *g, int x, int y, uint32_t fg, int level);
void g_frame(rb_gfx *g, rb_rect r, uint32_t c);
/* Restrict the clip to r (in drawing coordinates); returns the old clip. */
rb_rect g_push_clip(rb_gfx *g, rb_rect r);
/* Character-coded sprite rows (see code_color); ' ' and '.' are transparent. */
void g_coded(rb_gfx *g, int x, int y, const char *const *rows, int nrows, int mode);
uint32_t code_color(char c, int *ok);

/* Text. scale >= 1 magnifies glyph pixels (the "large" style). */
int tx_draw(rb_gfx *g, const rbf_font *f, int scale, int x, int baseline, const char *s, size_t len, uint32_t c);
int tx_width(const rbf_font *f, int scale, const char *s, size_t len);
/* Draw s shortened with an ellipsis to fit maxw; returns drawn width. */
int tx_draw_fit(rb_gfx *g, const rbf_font *f, int x, int baseline, int maxw, const char *s, uint32_t c);
int tx_fit_len(const rbf_font *f, const char *s, size_t len, int maxw, int *ellipsis);

/* Icons. */
const rbi_icon *icon_get(const char *name, int size);
int icon_index(const char *name);          /* -1 if not a known icon name */
const char *icon_name(int index);
/* dim: 0 normal, 1 disabled (50% toward the dialog gray), ICON_SELECTED (the Finder's
 * selected icon, as Mac OS 9 darkens it: largest channel halved, the others quartered). */
#define ICON_SELECTED 2
void icon_draw(rb_gfx *g, int index, int size, int x, int y, int dim);
void icon_draw_named(rb_gfx *g, const char *name, int size, int x, int y, int dim);   /* any table icon */
void cursor_draw(rb_gfx *g, int x, int y, rb_rect *bounds);
rb_rect cursor_bounds(int x, int y);

/* ---- Platinum controls (platinum.c) ---------------------------------------- */
enum { PT_NORMAL = 0, PT_PRESSED = 1, PT_DISABLED = 2 };
#define PT_TITLE_H 20     /* window frame top to content line */
#define PT_FRAME 6        /* side/bottom frame inset (content line inclusive) */

void pt_window(rb_gfx *g, rb_rect frame, const char *title, int titled, const rbf_font *f);
rb_rect pt_window_content(rb_rect frame, int titled);
int pt_window_extra_h(int titled);   /* frame height minus content height */
void pt_button(rb_gfx *g, rb_rect r, const char *label, int state, int is_default, const rbf_font *f);
void pt_checkbox(rb_gfx *g, int x, int y, int checked, int state);
void pt_radio(rb_gfx *g, int x, int y, int on, int state);
void pt_group(rb_gfx *g, rb_rect r, const char *label, const rbf_font *f, int disabled);
void pt_separator(rb_gfx *g, int x, int y, int w);
void pt_sunken(rb_gfx *g, rb_rect r, uint32_t fill);   /* field / list frame */
void pt_scrollbar(rb_gfx *g, rb_rect r, int total, int visible, int first, int pressed, int thumb_y_override);
int pt_thumb_pos(rb_rect r, int total, int visible, int first);   /* thumb top y */
void pt_bar_track(rb_rect bar, int *y, int *h);   /* the track between/above the arrows */
int pt_bar_arrow(rb_rect bar, int y);             /* SB_UP, SB_DOWN or SB_NONE at y */
#define PT_THUMB_H 17
void pt_popup(rb_gfx *g, rb_rect r, const char *text, int state, const rbf_font *f);
void pt_progress(rb_gfx *g, rb_rect r, double v, int indeterminate, int phase, int disabled);
void pt_focus_ring(rb_gfx *g, rb_rect r);
void pt_menu(rb_gfx *g, rb_rect r);
void pt_checkmark(rb_gfx *g, int x, int y, uint32_t c);
void pt_signal(rb_gfx *g, int x, int y, int bars, int dim);
void pt_arrow(rb_gfx *g, int cx, int y, int down, uint32_t c);   /* 7x4 triangle */
/* Mac OS 9.2.1 look: drawn with the bundle's control sprites when it has them all. */
int pt_v9(void);
void pt_theme_update(void);   /* assets.c, after loading or forgetting the bundle */
void pt_field(rb_gfx *g, rb_rect c, int disabled, int focused);     /* edit field frame + background */
void pt_listbox(rb_gfx *g, rb_rect r, int disabled, int focused);   /* list frame + background */
void pt_menu_row(rb_gfx *g, rb_rect row);                           /* highlighted menu item */
void pt_menu_check(rb_gfx *g, int x, int baseline, uint32_t c, const rbf_font *f);
int pt_check_text_dx(void);   /* checkbox/radio box left edge to its label */
int pt_field_text_dx(void);   /* edit field frame left edge to its text (8.1: 5, 9.2.1: 4) */

/* ---- Document tree (doc.c) ------------------------------------------------- */
enum {
    N_TEXT, N_SEPARATOR, N_GROUP, N_STACK, N_COLUMNS, N_LIST, N_FIELD, N_CHECKBOX,
    N_RADIO, N_POPUP, N_PROGRESS, N_QR, N_BUTTONS, N_BUTTON, N_ICON, N_SPACER,
    N_FACTS, N_METERS, N_BAND, N_BEVELS, N_SLIDER, N__COUNT
};
enum { ST_NORMAL, ST_EMPHASIS, ST_SMALL, ST_LARGE };
enum { AL_LEFT, AL_CENTER, AL_RIGHT };

typedef struct {
    char *id, *label, *detail;
    int icon;          /* icon index or -1 */
    int disabled;
    int signal;        /* -1 none, else 0..4 bars */
    int lock;
    int checked;       /* list: a check mark beside the item (the current network) */
    double share;      /* meters: the bar's filled share 0..1, -1 an empty bar */
} rb_opt;

typedef struct rb_node rb_node;
struct rb_node {
    int type;
    char *id;
    char *text;            /* text, label, button title */
    char *placeholder;
    int style, align;
    int disabled, is_default, secure, scrollbar;
    int click_activate;    /* list: one click on a row is an action (Wi-Fi networks) */
    int icon_view, columns;   /* list "view": "icons": a Finder icon view, `columns` wide */
    int ax, ay;            /* band children: "x"/"y" offsets in the band (-1: none) */
    int rule;              /* band: separator lines, RULE_TOP | RULE_BOTTOM */
    int pin;               /* band: kept at the window's bottom, above the buttons */
    double vmin, vmax, vstep, val;   /* slider */
    int icon2;             /* slider: the icon after the track (n->icon before it), -1 none */
    char *header;          /* icon view: the Finder window header strip ("8 items, …") */
    int has_checks;        /* list: items say "checked", so rows keep a check-mark column */
    char *reveals;         /* checkbox: id of the secure field it shows in plain text */
    struct rb_node *reveal_field, *revealer;   /* linked after parsing (checkbox <-> field) */
    int width_hint, height_hint, rows, maxlen, module, quiet;
    int icon, icon_size;
    rb_node **kids;
    int nkids;
    rb_opt *opts;
    int nopts;
    rb_node *parent;

    /* values */
    char *doc_value;       /* canonical JSON of the doc's "value" (3-way merge) */
    int sel;               /* list / radio / popup selection, -1 none */
    int checked;
    char *buf;             /* field text */
    int len, cap, caret, anchor, scroll_x;
    int scroll;            /* list first visible row */
    double progress;
    int indeterminate;
    int qr_w, qr_h;
    unsigned char *qr;

    /* layout (window coordinates before content scroll) */
    rb_rect r;             /* whole item */
    rb_rect ctl;           /* the control: field box, list frame, popup, check box hit area, button */
    int label_w;           /* label column width (0 = no label) */
    int label_h;           /* list title height */
    int run_w;             /* popup: shared width of its label column run */
    int nlines;
    int *lines;            /* text: pairs of (offset, length) */
    int scrolled;          /* inside the scrolling viewport */
};

typedef struct {
    rb_node *root;          /* N_STACK of top-level items */
    char *id, *title, *focus;
    int kind;               /* 0 window, 1 alert */
    int width;
    int alert_icon;
    int reset;
    int min_h;              /* "height": the least content height (pages that keep one size) */
} rb_doc;

int doc_parse(const char *json, size_t len, rb_doc *out, char *err, size_t errlen);
void doc_free(rb_doc *d);
void node_free(rb_node *n);
/* Depth-first visit; stops early if fn returns nonzero (returned). */
typedef int (*node_fn)(rb_node *n, void *ctx);
int node_walk(rb_node *n, node_fn fn, void *ctx);
rb_node *node_by_id(rb_node *root, const char *id);
int node_focusable(const rb_node *n);
int node_has_value(const rb_node *n);
const char *node_value_id(const rb_node *n);   /* list/radio/popup selected option id or NULL */
int opt_index(const rb_node *n, const char *id);
int field_set_text(rb_node *n, const char *s, size_t len);
int field_codepoints(const rb_node *n);
/* A secure field shows bullets unless its "reveals" checkbox is on. Showing the
 * text is local to the panel: the value is still only sent as before. */
static inline int field_masked(const rb_node *n)
{
    return n->secure && !(n->revealer && n->revealer->checked);
}

/* ---- The UI object ------------------------------------------------------- */
enum { TR_NONE, TR_BUTTON, TR_CHECK, TR_RADIO, TR_LIST, TR_LISTBAR, TR_FIELD, TR_MENU, TR_VBAR, TR_SLIDER };
enum { SB_NONE = 0, SB_UP = 1, SB_DOWN = 2, SB_PAGEUP = 3, SB_PAGEDOWN = 4, SB_THUMB = 5 };

struct rbui {
    rbui_emit_fn emit;
    void *emit_ctx;
    int cw, ch;
    uint64_t now;

    rb_doc doc;
    int have_doc;
    rb_node *pinned;        /* trailing top-level buttons row, pinned to the bottom */
    rb_node *pinned_band;   /* a "pin" band just before it (Sound's Main Volume), also kept there */

    const rbf_font *f_sys, *f_reg, *f_small;
    const rbf_font *f_tiny;   /* Geneva 9 (bevel button labels); f_small without the bundle */

    /* geometry */
    rb_rect frame, content, view, vbar, icon_r;
    int content_h;          /* height of everything inside the viewport */
    int scroll, scrollable;

    /* focus */
    rb_node *focus;
    int focus_opt;          /* radio: option with the focus ring */

    /* pointer */
    int px, py;
    unsigned buttons;
    int track, track_part, track_inside, track_opt, track_grab;
    rb_node *tnode;
    uint64_t repeat_at;
    rb_node *click_node;
    int click_row;
    uint64_t click_time;
    /* After a click on a click-to-act list row, the second click of a double-click
     * lands on whatever replaced it: swallow a press this close in time and place. */
    uint64_t swallow_until;
    int swallow_x, swallow_y;

    /* popup menu */
    rb_node *menu;
    rb_rect menu_r;
    int menu_hi, menu_first, menu_rows, menu_sticky, menu_moved;
    int menu_arrows;        /* menu taller than the canvas: scroll arrows shown */
    uint64_t menu_open_time;

    /* animation */
    rb_node *flash;
    uint64_t flash_until;
    int caret_on;
    uint64_t caret_next;
    int barber;
    uint64_t barber_next;
    char ta[32];
    int ta_len;
    uint64_t ta_time;

    /* damage */
    int dmg;
    rb_rect damage;

    /* queued event JSON strings, delivered when the entry point returns */
    char **evq;
    int evq_n, evq_cap, evq_flushing;
};

/* rbui.c */
void ui_damage(rbui *ui, rb_rect r);
void ui_damage_node(rbui *ui, rb_node *n);
void ui_damage_all(rbui *ui);
rb_rect ui_node_screen(const rbui *ui, const rb_node *n, rb_rect r);
void ui_emit_change(rbui *ui, rb_node *n);
void ui_emit_action(rbui *ui, rb_node *button);
void ui_set_focus(rbui *ui, rb_node *n);
void ui_set_focus_ex(rbui *ui, rb_node *n, int reveal);   /* reveal=0 for clicks */
void ui_reveal(rbui *ui, rb_node *n);
void ui_caret_reset(rbui *ui);

/* layout.c */
void layout_doc(rbui *ui);
int layout_text_lines(rbui *ui, rb_node *n, int width);
int style_line_h(const rbui *ui, int style);
const rbf_font *style_font(const rbui *ui, int style, int *scale);
int list_row_h(void);
int list_visible_rows(const rb_node *n);
rb_rect list_rows_rect(const rb_node *n);
rb_rect list_bar_rect(const rb_node *n);
int list_has_bar(const rb_node *n);
void list_clamp_scroll(rb_node *n);
void list_reveal(rb_node *n, int row);
/* Icon view (a list with "view": "icons"), measured from Mac OS 9.2.2's Finder: 128 x 64
 * cells of a 32-pixel icon with its name 3 pixels below in Geneva 10, under a header strip. */
#define ICON_CELL_W 128
#define ICON_CELL_H 64
/* About This Computer (Mac OS 9.2.2): "facts" rows of a Geneva 10 bold label and its value,
 * 16 px apart; "meters", its list of rows 19 px apart (a white line between them), each a
 * 16 px icon, a name, a figure and a 185 px progress bar 9 px from the right. */
/* Monitors (Mac OS 9.2.2): a "band" runs to the window's edges with Appearance separators;
 * "bevels" are its 40 x 40 bevel buttons, 55 px apart, a Geneva 9 label 51 px below each
 * top, centred in 43 px.  Sound's "slider": a 13 x 16 thumb 4 px above a 7 px track
 * (137 px by default); its 16 px end icons 9 px after the label and 7 px after the track,
 * the track 22 px after the first icon, the label's baseline 12 px below the thumb's top. */
#define RULE_TOP 1
#define RULE_BOTTOM 2
#define BEVEL_W 40
#define BEVEL_PITCH 55
#define BEVEL_H 54
#define SLIDER_THUMB_W 13
#define SLIDER_H 16
#define SLIDER_TRACK_W 137
#define FACTS_ROW_H 16
#define METER_ROW_H 19
#define METER_BAR_W 185
#define ICON_HEADER_H 21   /* white line, 18 rows of gray, a gray shade line and a black line */
int icons_rows(const rb_node *n);
rb_rect icons_area(const rb_node *n);                 /* the white area below the header */
rb_rect icons_icon_rect(const rb_node *n, int i);     /* item i's 32 x 32 icon */
int icons_index_at(const rb_node *n, int x, int y);   /* item under (x, y) in node coordinates, -1 none */
rb_rect field_inner(const rb_node *n);
void field_fix_scroll(rbui *ui, rb_node *n);
int field_x_to_offset(rbui *ui, rb_node *n, int x);
int field_offset_x(rbui *ui, rb_node *n, int off);   /* x of offset within the text */
rb_rect radio_opt_rect(const rbui *ui, const rb_node *n, int i);
rb_rect bevel_rect(const rb_node *n, int i);                   /* layout.c */
typedef struct { int label_x, lo_x, hi_x, thumb_x; rb_rect track; } slider_parts;
slider_parts slider_geom(const rbui *ui, const rb_node *n);   /* layout.c */
double slider_value_at(const rbui *ui, const rb_node *n, int thumb_left);
void pt_bevel(rb_gfx *g, rb_rect r, int pressed);
void pt_slider(rb_gfx *g, rb_rect track, int thumb_x, int thumb_y, int pressed, int disabled);
int button_width(const rbui *ui, const rb_node *b);
int qr_module(const rb_node *n, int avail_w);
void menu_layout(rbui *ui);

/* render.c */
void render_doc(rbui *ui, rb_gfx *g);

/* input.c */
void input_key(rbui *ui, int key, const char *utf8, unsigned mods);
void input_pointer(rbui *ui, int x, int y, unsigned buttons);
void input_tick(rbui *ui);
void input_wheel(rbui *ui, int dy);
void menu_open(rbui *ui, rb_node *n, int sticky);
void menu_close(rbui *ui);
void activate_button(rbui *ui, rb_node *b, int flash);

#define RB_MARGIN 12
#define RB_BUTTON_H 20
#define RB_BUTTON_MIN_W 59
#define RB_FIELD_H 22
#define RB_POPUP_H 20
#define RB_CTL_ROW_H 18
#define RB_SCROLLBAR_W 16
#define RB_MENU_ITEM_H 18
#define RB_CARET_MS 530
#define RB_FLASH_MS 140
#define RB_DOUBLE_MS 500
#define RB_TYPEAHEAD_MS 1000
#define RB_BARBER_MS 50
#define RB_REPEAT_FIRST_MS 300
#define RB_REPEAT_MS 60

#endif
