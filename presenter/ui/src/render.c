/* render.c - draw the laid-out document with the Platinum controls */
#include <stdlib.h>
#include <string.h>

#include "rbui_internal.h"

static uint32_t fg(int disabled) { return disabled ? g_pal.disabled : g_pal.text; }

static void draw_label(rbui *ui, rb_gfx *g, const rb_node *n, int baseline, int disabled)
{
    /* Right-aligned in the shared label column, or left-aligned when the
     * control has no column (label_w == 0 only happens without a label). */
    if (!n->text || !n->text[0]) return;
    int tw = tx_width(ui->f_sys, 1, n->text, strlen(n->text));
    int maxw = n->label_w > 0 ? n->label_w : tw;
    int x = n->r.x + (tw < maxw ? maxw - tw : 0);
    tx_draw_fit(g, ui->f_sys, x, baseline, maxw, n->text, fg(disabled));
}

static void draw_text(rbui *ui, rb_gfx *g, rb_node *n, int disabled)
{
    int scale;
    const rbf_font *f = style_font(ui, n->style, &scale);
    int lh = style_line_h(ui, n->style);
    for (int i = 0; i < n->nlines; ++i) {
        const char *s = n->text + n->lines[2 * i];
        size_t len = (size_t)n->lines[2 * i + 1];
        int w = tx_width(f, scale, s, len), x = n->r.x;
        if (n->align == AL_CENTER) x += (n->r.w - w) / 2;
        else if (n->align == AL_RIGHT) x += n->r.w - w;
        tx_draw(g, f, scale, x, n->r.y + i * lh + f->ascent * scale, s, len, fg(disabled));
    }
}

static void draw_button(rbui *ui, rb_gfx *g, rb_node *n, int disabled)
{
    int pressed = (ui->track == TR_BUTTON && ui->tnode == n && ui->track_inside) || ui->flash == n;
    int state = disabled ? PT_DISABLED : pressed ? PT_PRESSED : PT_NORMAL;
    pt_button(g, n->ctl, n->text, state, n->is_default, ui->f_sys);
    if (ui->focus == n && !disabled) pt_focus_ring(g, n->is_default ? r_out(n->ctl, 3) : n->ctl);
}

static void draw_checkbox(rbui *ui, rb_gfx *g, rb_node *n, int disabled)
{
    int pressed = ui->track == TR_CHECK && ui->tnode == n && ui->track_inside;
    int bx = n->ctl.x + 2, by = n->ctl.y + 3;
    pt_checkbox(g, bx, by, n->checked, disabled ? PT_DISABLED : pressed ? PT_PRESSED : PT_NORMAL);
    if (n->text) tx_draw_fit(g, ui->f_sys, bx + pt_check_text_dx(), n->ctl.y + 13, n->ctl.w - 22, n->text, fg(disabled));
    if (ui->focus == n && !disabled) pt_focus_ring(g, rr(bx, by, 12, 12));
}

static void draw_radio(rbui *ui, rb_gfx *g, rb_node *n, int disabled)
{
    draw_label(ui, g, n, n->ctl.y + 13, disabled);
    for (int i = 0; i < n->nopts; ++i) {
        rb_rect o = radio_opt_rect(ui, n, i);
        int dis = disabled || n->opts[i].disabled;
        int pressed = ui->track == TR_RADIO && ui->tnode == n && ui->track_opt == i && ui->track_inside;
        pt_radio(g, o.x + 2, o.y + 3, n->sel == i, dis ? PT_DISABLED : pressed ? PT_PRESSED : PT_NORMAL);
        tx_draw_fit(g, ui->f_sys, o.x + 2 + pt_check_text_dx(), o.y + 13, o.w - 22, n->opts[i].label, fg(dis));
        if (ui->focus == n && !disabled && i == ui->focus_opt) pt_focus_ring(g, rr(o.x + 2, o.y + 3, 12, 12));
    }
}

static void draw_popup(rbui *ui, rb_gfx *g, rb_node *n, int disabled)
{
    draw_label(ui, g, n, n->ctl.y + (n->ctl.h - ui->f_sys->cap) / 2 + ui->f_sys->cap, disabled);
    const char *cur = n->sel >= 0 && n->sel < n->nopts ? n->opts[n->sel].label : "";
    int state = disabled ? PT_DISABLED : (ui->menu == n ? PT_PRESSED : PT_NORMAL);
    pt_popup(g, n->ctl, cur, state, ui->f_sys);
    if (ui->focus == n && !disabled && ui->menu != n) pt_focus_ring(g, n->ctl);
}

static void draw_field(rbui *ui, rb_gfx *g, rb_node *n, int disabled)
{
    const rbf_font *f = ui->f_sys;
    rb_rect c = n->ctl;
    int baseline = c.y + (c.h - f->cap) / 2 + f->cap;
    draw_label(ui, g, n, baseline, disabled);
    int focused = ui->focus == n && !disabled;
    pt_field(g, c, disabled, focused);
    rb_rect in = field_inner(n);
    rb_rect old = g_push_clip(g, in);
    int tx = c.x + pt_field_text_dx() - n->scroll_x;
    if (focused && n->caret != n->anchor) {
        int a = n->caret < n->anchor ? n->caret : n->anchor, b = n->caret ^ n->anchor ^ a;
        int x0 = tx + field_offset_x(ui, n, a), x1 = tx + field_offset_x(ui, n, b);
        g_fill(g, x0, c.y + 3, x1 - x0, c.h - 6, g_pal.hilite);
    }
    if (n->len == 0 && n->placeholder) {
        tx_draw(g, f, 1, c.x + pt_field_text_dx(), baseline, n->placeholder, strlen(n->placeholder), g_pal.placeholder);
    } else if (field_masked(n)) {
        int adv = rbf_advance(f, 0x2022), count = field_codepoints(n);
        for (int i = 0; i < count; ++i)
            tx_draw(g, f, 1, tx + i * adv, baseline, "\xE2\x80\xA2", 3, fg(disabled));
    } else {
        tx_draw(g, f, 1, tx, baseline, n->buf, (size_t)n->len, fg(disabled));
    }
    if (focused && ui->caret_on && n->caret == n->anchor) {
        int x = tx + field_offset_x(ui, n, n->caret) - 1;
        if (x < c.x + 4) x = c.x + 4;
        if (n->len == 0) x = c.x + 3;   /* keep a gap before placeholder text */
        g_vline(g, x, baseline - f->cap - 2, f->cap + 5, C_BLACK);
    }
    g->clip = old;
    if (focused && !pt_v9()) pt_focus_ring(g, r_out(c, 1));   /* 9.2.1: pt_field drew it */
}

#define LIST_CHECK_W 16   /* the menu's 20-pixel text indent, less the list's own 4 */

static void draw_list(rbui *ui, rb_gfx *g, rb_node *n, int disabled)
{
    const rbf_font *f = ui->f_sys;
    if (n->label_h) tx_draw_fit(g, f, n->r.x, n->r.y + 13, n->r.w, n->text, fg(disabled));
    pt_listbox(g, n->ctl, disabled, ui->focus == n && !disabled);
    rb_rect rows = list_rows_rect(n);
    int vis = list_visible_rows(n), rh = list_row_h();
    rb_rect old = g_push_clip(g, rows);
    for (int i = 0; i < vis && n->scroll + i < n->nopts; ++i) {
        int idx = n->scroll + i;
        const rb_opt *o = &n->opts[idx];
        rb_rect row = rr(rows.x, rows.y + i * rh, rows.w, rh);
        int dis = disabled || o->disabled;
        if (idx == n->sel) g_fillr(g, row, disabled ? g_pal.hilite_off : g_pal.hilite);
        /* A check-mark column, as beside the current item of a menu (the joined network). */
        int ck = n->has_checks ? LIST_CHECK_W : 0;
        if (o->checked) pt_menu_check(g, row.x + 2, row.y + 13, fg(dis), f);
        int x = row.x + 4 + ck;
        if (o->icon >= 0) {
            icon_draw(g, o->icon, 16, row.x + 3 + ck, row.y + 1, dis);
            x = row.x + 24 + ck;
        }
        int right = row.x + row.w - 6;
        if (o->signal >= 0) {
            right -= 15;
            pt_signal(g, right, row.y + 3, o->signal, dis);
            right -= 6;
        }
        if (o->lock) {
            int li = icon_index("lock");
            if (icon_get("lock", 16)) {
                right -= 12;
                icon_draw(g, li, 16, right - 2, row.y + 1, dis);
                right -= 6;
            }
        }
        if (o->detail && o->detail[0]) {
            int dw = tx_width(ui->f_small, 1, o->detail, strlen(o->detail));
            int maxd = (right - x) / 2;
            if (dw > maxd) dw = maxd;
            tx_draw_fit(g, ui->f_small, right - dw, row.y + 13, dw, o->detail, dis ? g_pal.dim_disabled : g_pal.dim);
            right -= dw + 10;
        }
        tx_draw_fit(g, f, x, row.y + 13, right - x, o->label, fg(dis));
    }
    g->clip = old;
    if (list_has_bar(n)) {
        int pressed = ui->track == TR_LISTBAR && ui->tnode == n && ui->track_inside ? ui->track_part : 0;
        int thumb = -1;
        if (ui->track == TR_LISTBAR && ui->tnode == n && ui->track_part == SB_THUMB) thumb = -1;
        pt_scrollbar(g, list_bar_rect(n), n->nopts, vis, n->scroll, pressed, thumb);
    }
    if (ui->focus == n && !disabled && !pt_v9()) pt_focus_ring(g, r_out(n->ctl, 1));
}

/* A Finder icon view, measured on Mac OS 9.2.2 (Control Panels, icon view): the header strip
 * (white line, #DDDDDD, #AAAAAA shade, black line) with its text centred in Geneva 10; each
 * icon at 32 pixels, its name 3 pixels below in Geneva 10, centred; the selected one has the
 * icon darkened (largest channel halved, others quartered) and its name white in a black box
 * 3 pixels wider than the ink on each side, from 10 above the baseline to 3 below. No focus
 * ring, as in the Finder. */
static void draw_icons(rbui *ui, rb_gfx *g, rb_node *n, int disabled)
{
    const rbf_font *f = ui->f_small;
    if (n->label_h) tx_draw_fit(g, ui->f_sys, n->r.x, n->r.y + 13, n->r.w, n->text, fg(disabled));
    pt_listbox(g, n->ctl, disabled, 0);
    rb_rect in = rr(n->ctl.x + 1, n->ctl.y + 1, n->ctl.w - 2, n->ctl.h - 2);
    rb_rect old = g_push_clip(g, in);
    g_fillr(g, in, C_WHITE);
    if (n->header) {
        g_fill(g, in.x, in.y + 1, in.w, 18, 0xDDDDDDu);
        g_hline(g, in.x, in.y, in.w, C_WHITE);
        g_vline(g, in.x, in.y, 19, C_WHITE);
        g_hline(g, in.x, in.y + 19, in.w, 0xAAAAAAu);
        g_hline(g, in.x, in.y + 20, in.w, C_BLACK);
        int tw = tx_width(f, 1, n->header, strlen(n->header));
        if (tw > in.w - 8) tw = in.w - 8;
        tx_draw_fit(g, f, in.x + (in.w - tw) / 2, in.y + 14, tw, n->header, disabled ? C_DISABLED : C_BLACK);
    }
    for (int i = 0; i < n->nopts; ++i) {
        const rb_opt *o = &n->opts[i];
        rb_rect ic = icons_icon_rect(n, i);
        int sel = i == n->sel && !disabled, dis = disabled || o->disabled;
        if (o->icon >= 0) icon_draw(g, o->icon, 32, ic.x, ic.y, sel ? ICON_SELECTED : dis);
        int maxw = ICON_CELL_W - 8, tw = tx_width(f, 1, o->label, strlen(o->label));
        if (tw > maxw) tw = maxw;
        int tx = ic.x + 16 - (tw + 1) / 2, baseline = ic.y + 42;
        if (sel) g_fill(g, tx - 2, baseline - 10, tw + 4, 13, C_BLACK);   /* ink + 3 each side */
        tx_draw_fit(g, f, tx, baseline, tw, o->label, sel ? C_WHITE : dis ? C_DISABLED : C_BLACK);
    }
    g->clip = old;
}

/* QuickDraw's synthetic bold: each glyph drawn twice, one pixel apart, advancing a pixel
 * more (the bundle's geneva-10-bold is the same strike, made the same way). */
static int draw_bold(rb_gfx *g, const rbf_font *f, int x, int baseline, int maxw, const char *s, uint32_t c)
{
    const rbf_font *b = assets_font("small10b");
    if (b && f == rbf_get("small10")) return tx_draw_fit(g, b, x, baseline, maxw, s, c);
    size_t i = 0, len = strlen(s);
    int x0 = x;
    while (i < len) {
        size_t j = i;
        uint32_t cp = rbf_utf8_next(s, len, &j);
        int a = rbf_advance(f, cp) + 1;
        if (x + a - x0 > maxw) break;
        tx_draw(g, f, 1, x, baseline, s + i, j - i, c);
        tx_draw(g, f, 1, x + 1, baseline, s + i, j - i, c);
        x += a;
        i = j;
    }
    return x - x0;
}

static int bold_width(const rbf_font *f, const char *s)
{
    const rbf_font *b = assets_font("small10b");
    if (b && f == rbf_get("small10")) return tx_width(b, 1, s, strlen(s));
    size_t i = 0, len = strlen(s);
    int w = 0;
    while (i < len) w += rbf_advance(f, rbf_utf8_next(s, len, &i)) + 1;
    return w;
}

/* About This Computer's upper part: "Version:" in bold, then its value. */
static void draw_facts(rbui *ui, rb_gfx *g, rb_node *n, int disabled)
{
    const rbf_font *f = ui->f_small;
    uint32_t c = fg(disabled);
    for (int i = 0; i < n->nopts; ++i) {
        const rb_opt *o = &n->opts[i];
        int base = n->ctl.y + 12 + i * FACTS_ROW_H;
        int lw = bold_width(f, o->label);
        if (lw > n->ctl.w) lw = n->ctl.w;
        draw_bold(g, f, n->ctl.x, base, lw, o->label, c);
        int vx = n->ctl.x + lw + 8;
        if (o->detail && vx < n->ctl.x + n->ctl.w) tx_draw_fit(g, f, vx, base, n->ctl.x + n->ctl.w - vx, o->detail, c);
    }
}

/* About This Computer's list: on #EEEEEE, each row its 16 px icon, name, figure and bar. */
static void draw_meters(rbui *ui, rb_gfx *g, rb_node *n, int disabled)
{
    if (!n->nopts) return;
    const rbf_font *f = ui->f_small;
    pt_listbox(g, n->ctl, disabled, 0);
    rb_rect in = rr(n->ctl.x + 1, n->ctl.y + 1, n->ctl.w - 2, n->ctl.h - 2);
    rb_rect old = g_push_clip(g, in);
    g_fillr(g, in, 0xEEEEEEu);
    int bw = METER_BAR_W;
    if (bw > in.w / 2) bw = in.w / 2;
    for (int i = 0; i < n->nopts; ++i) {
        const rb_opt *o = &n->opts[i];
        int t = in.y + i * METER_ROW_H, l = in.x;
        int bx = in.x + in.w - 9 - bw;
        if (i + 1 < n->nopts) g_hline(g, in.x, t + METER_ROW_H - 1, in.w, C_WHITE);
        if (o->icon >= 0) icon_draw(g, o->icon, 16, l + 16, t + 1, disabled);
        pt_progress(g, rr(bx + 1, t + 3, bw - 2, 12), o->share > 0 ? o->share : 0, 0, 0, disabled);
        g_pixel(g, bx + bw - 1, t + 2, C_WHITE);   /* the highlight's corners, white on #EEEEEE */
        g_pixel(g, bx, t + 15, C_WHITE);
        int vw = o->detail ? tx_width(f, 1, o->detail, strlen(o->detail)) : 0;
        int vx = bx - 9 - vw;
        if (o->detail) tx_draw(g, f, 1, vx, t + 13, o->detail, strlen(o->detail), fg(disabled));
        int nx = l + 41, room = (o->detail ? vx - 8 : bx - 9) - nx;
        if (room > 0) tx_draw_fit(g, f, nx, t + 13, room, o->label, fg(disabled));
    }
    g->clip = old;
}

/* Monitors' top band and Sound's volume band: Appearance separators across the window. */
static void draw_band(rb_gfx *g, rb_node *n)
{
    if (n->rule & RULE_TOP) pt_separator(g, n->ctl.x, n->ctl.y, n->ctl.w - 1);
    if (n->rule & RULE_BOTTOM) pt_separator(g, n->ctl.x, n->ctl.y + n->ctl.h - 2, n->ctl.w - 1);
}

/* Monitors' bevel buttons: the chosen one stays pressed and draws its icon selected (each
 * channel darkened as the Finder darkens a selected icon); labels in Geneva 9 below. */
static void draw_bevels(rbui *ui, rb_gfx *g, rb_node *n, int disabled)
{
    for (int i = 0; i < n->nopts; ++i) {
        const rb_opt *o = &n->opts[i];
        rb_rect b = bevel_rect(n, i);
        int dis = disabled || o->disabled;
        int pressed = !dis && (n->sel == i || (ui->track == TR_RADIO && ui->tnode == n && ui->track_opt == i &&
                                                ui->track_inside));
        pt_bevel(g, b, pressed);
        if (o->icon >= 0) icon_draw(g, o->icon, BEVEL_W, b.x, b.y, pressed ? ICON_SELECTED : dis);
        int tw = tx_width(ui->f_tiny, 1, o->label, strlen(o->label));
        int lx = b.x + (43 - tw) / 2;
        if (tw > BEVEL_PITCH - 4) lx = b.x + (BEVEL_W - (BEVEL_PITCH - 4)) / 2;
        tx_draw_fit(g, ui->f_tiny, lx, b.y + 51, BEVEL_PITCH - 4, o->label, fg(dis));
        if (ui->focus == n && !disabled && i == ui->focus_opt) pt_focus_ring(g, b);
    }
}

/* Sound's Main Volume: label, end icons, track and the plain thumb. */
static void draw_slider(rbui *ui, rb_gfx *g, rb_node *n, int disabled)
{
    slider_parts s = slider_geom(ui, n);
    int y = n->r.y;
    if (n->text && n->text[0]) tx_draw(g, ui->f_sys, 1, s.label_x, y + 12, n->text, strlen(n->text), fg(disabled));
    if (s.lo_x >= 0) icon_draw(g, n->icon, 16, s.lo_x, y, disabled);
    if (s.hi_x >= 0) icon_draw(g, n->icon2, 16, s.hi_x, y, disabled);
    int pressed = ui->track == TR_SLIDER && ui->tnode == n;
    pt_slider(g, s.track, s.thumb_x, y, pressed, disabled);
    if (ui->focus == n && !disabled) pt_focus_ring(g, rr(s.thumb_x, y, SLIDER_THUMB_W, SLIDER_H));
}

static void draw_qr(rb_gfx *g, rb_node *n)
{
    int m = n->ctl.w / (n->qr_w + 2 * n->quiet);
    if (m < 1) m = 1;
    g_fillr(g, n->ctl, C_WHITE);
    int ox = n->ctl.x + n->quiet * m, oy = n->ctl.y + n->quiet * m;
    for (int y = 0; y < n->qr_h; ++y)
        for (int x = 0; x < n->qr_w; ++x)
            if (n->qr[y * n->qr_w + x]) g_fill(g, ox + x * m, oy + y * m, m, m, C_BLACK);
}

static void draw_node(rbui *ui, rb_gfx *g, rb_node *n, int pdis)
{
    int dis = pdis || n->disabled;
    switch (n->type) {
    case N_TEXT: draw_text(ui, g, n, dis); break;
    case N_SEPARATOR: pt_separator(g, n->r.x, n->r.y, n->r.w - 1); break;
    case N_GROUP: pt_group(g, n->ctl, n->text, ui->f_sys, dis); break;
    case N_BUTTON: draw_button(ui, g, n, dis); break;
    case N_CHECKBOX: draw_checkbox(ui, g, n, dis); break;
    case N_RADIO: draw_radio(ui, g, n, dis); break;
    case N_POPUP: draw_popup(ui, g, n, dis); break;
    case N_FIELD: draw_field(ui, g, n, dis); break;
    case N_LIST: if (n->icon_view) draw_icons(ui, g, n, dis); else draw_list(ui, g, n, dis); break;
    case N_PROGRESS: pt_progress(g, n->ctl, n->progress, n->indeterminate, ui->barber, dis); break;
    case N_QR: draw_qr(g, n); break;
    case N_FACTS: draw_facts(ui, g, n, dis); break;
    case N_BAND: draw_band(g, n); break;
    case N_BEVELS: draw_bevels(ui, g, n, dis); break;
    case N_SLIDER: draw_slider(ui, g, n, dis); break;
    case N_METERS: draw_meters(ui, g, n, dis); break;
    case N_ICON: icon_draw(g, n->icon, n->icon_size, n->ctl.x, n->ctl.y, dis); break;
    default: break;
    }
    for (int i = 0; i < n->nkids; ++i) draw_node(ui, g, n->kids[i], dis);
}

static void draw_menu(rbui *ui, rb_gfx *g)
{
    rb_node *n = ui->menu;
    if (!n) return;
    rb_rect m = ui->menu_r;
    pt_menu(g, m);
    int y = m.y + 3, rows = ui->menu_rows, first = ui->menu_first;
    if (ui->menu_arrows) {
        if (first > 0) pt_arrow(g, m.x + m.w / 2, y + 7, 0, C_BLACK);
        y += RB_MENU_ITEM_H;
        rows -= 2;
    }
    for (int i = 0; i < rows && first + i < n->nopts; ++i) {
        int idx = first + i;
        rb_rect row = rr(m.x + 1, y + i * RB_MENU_ITEM_H, m.w - 2, RB_MENU_ITEM_H);
        int dis = n->opts[idx].disabled;
        uint32_t col = dis ? g_pal.disabled : g_pal.text;
        if (idx == ui->menu_hi && !dis) {
            pt_menu_row(g, row);
            col = C_WHITE;
        }
        if (idx == n->sel) pt_menu_check(g, row.x + 2, row.y + 13, col, ui->f_sys);
        int tx = pt_v9() ? 20 : 19;   /* 9.2.1: the web menu's 20-pixel indent */
        tx_draw_fit(g, ui->f_sys, row.x + tx, row.y + 13, row.w - tx - 12, n->opts[idx].label, col);
    }
    if (ui->menu_arrows && first + rows < n->nopts)
        pt_arrow(g, m.x + m.w / 2, y + rows * RB_MENU_ITEM_H + 7, 1, C_BLACK);
}

void render_doc(rbui *ui, rb_gfx *g)
{
    if (!ui->have_doc) return;
    rb_doc *d = &ui->doc;
    int titled = d->kind == 0 || (d->title && d->title[0]);
    pt_window(g, ui->frame, d->title, titled, ui->f_sys);

    /* Scrolled viewport: everything but the pinned button row. */
    rb_gfx v = *g;
    v.clip = r_and(g->clip, ui->view);
    if (ui->scrollable) v.clip = r_and(v.clip, rr(ui->view.x, ui->view.y, ui->view.w - RB_SCROLLBAR_W, ui->view.h));
    v.dy = g->dy - ui->scroll;
    if (d->alert_icon >= 0 && d->kind == 1) icon_draw(&v, d->alert_icon, 32, ui->icon_r.x, ui->icon_r.y, 0);
    rb_node *root = d->root;
    for (int i = 0; i < root->nkids; ++i)
        if (root->kids[i] != ui->pinned && root->kids[i] != ui->pinned_band) draw_node(ui, &v, root->kids[i], 0);
    if (ui->scrollable) {
        int pressed = ui->track == TR_VBAR && ui->track_inside ? ui->track_part : 0;
        pt_scrollbar(g, ui->vbar, ui->content_h, ui->view.h, ui->scroll, pressed, -1);
        if (ui->pinned) pt_separator(g, ui->content.x, ui->view.y + ui->view.h, ui->content.w - 1);
    }
    if (ui->pinned || ui->pinned_band) {
        rb_rect old = g_push_clip(g, ui->content);
        if (ui->pinned) draw_node(ui, g, ui->pinned, 0);
        if (ui->pinned_band) draw_node(ui, g, ui->pinned_band, 0);
        g->clip = old;
    }
    draw_menu(ui, g);
}
