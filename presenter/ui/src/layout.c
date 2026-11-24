/*
 * layout.c - place the document's items in Mac OS 8/9 dialog proportions
 *
 * Window content uses 12-pixel margins; push buttons are 20 pixels high and
 * at least 59 wide, right-aligned with 12 pixels between them; labels of
 * consecutive fields/popups/radio groups share one right-aligned column.
 */
#include <stdlib.h>
#include <string.h>

#include "rbui_internal.h"

#define LIST_ROW_H 18
#define BUTTON_GAP 12
#define COLUMN_GAP 16
#define LABEL_GAP 8
#define ALERT_ICON_GAP 16

/* ---- Fonts and styles -------------------------------------------------------- */

const rbf_font *style_font(const rbui *ui, int style, int *scale)
{
    *scale = 1;
    switch (style) {
    case ST_EMPHASIS: return ui->f_sys;
    case ST_SMALL: return ui->f_small;
    case ST_LARGE: {
        const rbf_font *big = assets_font("large24");   /* the bundle's Charcoal 24 */
        if (big) return big;
        *scale = 2;
        return ui->f_sys;
    }
    default: return ui->f_reg;
    }
}

int style_line_h(const rbui *ui, int style)
{
    int scale;
    const rbf_font *f = style_font(ui, style, &scale);
    return (f->ascent + f->descent) * scale;
}

/* ---- Text wrapping ------------------------------------------------------------ */

static int push_line(rb_node *n, int *cap, int off, int len)
{
    if (n->nlines * 2 + 2 > *cap) {
        int nc = *cap ? *cap * 2 : 16;
        int *nl = realloc(n->lines, (size_t)nc * sizeof(int));
        if (!nl) return -1;
        n->lines = nl;
        *cap = nc;
    }
    n->lines[n->nlines * 2] = off;
    n->lines[n->nlines * 2 + 1] = len;
    n->nlines++;
    return 0;
}

int layout_text_lines(rbui *ui, rb_node *n, int width)
{
    int scale;
    const rbf_font *f = style_font(ui, n->style, &scale);
    const char *s = n->text ? n->text : "";
    int len = (int)strlen(s), cap = 0;
    free(n->lines);
    n->lines = NULL;
    n->nlines = 0;
    if (width < 8 * scale) width = 8 * scale;
    int para = 0;
    while (para <= len) {
        int end = para;
        while (end < len && s[end] != '\n') ++end;
        /* Greedy word wrap of s[para, end). */
        int line = para;
        if (line == end && push_line(n, &cap, line, 0)) return -1;
        while (line < end) {
            int i = line, last_break = -1, w = 0;
            while (i < end) {
                size_t j = (size_t)i;
                uint32_t cp = rbf_utf8_next(s, (size_t)end, &j);
                int a = rbf_advance(f, cp) * scale;
                if (w + a > width && i > line) break;
                w += a;
                if (cp == ' ') last_break = (int)j;
                else if ((cp == '/' || cp == '-' || cp == ':' || cp == ',') && (int)j < end) last_break = (int)j;
                i = (int)j;
            }
            int brk = i;
            if (i < end && last_break > line) brk = last_break;
            int l = brk - line;
            while (l > 0 && s[line + l - 1] == ' ') --l;   /* trailing spaces */
            if (push_line(n, &cap, line, l)) return -1;
            line = brk;
            while (line < end && s[line] == ' ') ++line;
        }
        para = end + 1;
    }
    return 0;
}

/* ---- Lists --------------------------------------------------------------------- */

int list_row_h(void) { return LIST_ROW_H; }

int list_visible_rows(const rb_node *n)
{
    int v = (n->ctl.h - 2) / LIST_ROW_H;
    return v < 1 ? 1 : v;
}

int list_has_bar(const rb_node *n) { return n->scrollbar || n->nopts > list_visible_rows(n); }

rb_rect list_bar_rect(const rb_node *n)
{
    return rr(n->ctl.x + n->ctl.w - RB_SCROLLBAR_W, n->ctl.y, RB_SCROLLBAR_W, n->ctl.h);
}

rb_rect list_rows_rect(const rb_node *n)
{
    int bar = list_has_bar(n) ? RB_SCROLLBAR_W - 1 : 0;
    return rr(n->ctl.x + 1, n->ctl.y + 1, n->ctl.w - 2 - bar, n->ctl.h - 2);
}

void list_clamp_scroll(rb_node *n)
{
    int max = n->nopts - list_visible_rows(n);
    if (n->scroll > max) n->scroll = max;
    if (n->scroll < 0) n->scroll = 0;
}

void list_reveal(rb_node *n, int row)
{
    if (row < 0) return;
    int vis = list_visible_rows(n);
    if (row < n->scroll) n->scroll = row;
    if (row >= n->scroll + vis) n->scroll = row - vis + 1;
    list_clamp_scroll(n);
}

/* ---- Icon view (a list with "view": "icons") ------------------------------------- */

int icons_rows(const rb_node *n)
{
    int c = n->columns > 0 ? n->columns : 4;
    return (n->nopts + c - 1) / c;
}

rb_rect icons_area(const rb_node *n)
{
    int hh = n->header ? ICON_HEADER_H : 0;
    return rr(n->ctl.x + 1, n->ctl.y + 1 + hh, n->ctl.w - 2, n->ctl.h - 2 - hh);
}

rb_rect icons_icon_rect(const rb_node *n, int i)
{
    /* Cells side by side, centred in the area: with `columns` x 128 - 32 pixels across, the
     * first icon's centre is 48 pixels from the edge, as in a Finder window. */
    int c = n->columns > 0 ? n->columns : 4;
    rb_rect a = icons_area(n);
    int col = i % c, row = i / c;
    int cx2 = a.w + (2 * col - (c - 1)) * ICON_CELL_W;   /* twice the centre, from a.x */
    return rr(a.x + cx2 / 2 - 16, a.y + 4 + row * ICON_CELL_H, 32, 32);
}

int icons_index_at(const rb_node *n, int x, int y)
{
    /* The icon, or the line of its name below it (the Finder selects on either). */
    for (int i = 0; i < n->nopts; ++i) {
        rb_rect ic = icons_icon_rect(n, i);
        if (r_has(ic, x, y) || r_has(rr(ic.x + 16 - (ICON_CELL_W - 8) / 2, ic.y + 32, ICON_CELL_W - 8, 13), x, y))
            return i;
    }
    return -1;
}

/* ---- Fields ------------------------------------------------------------------ */

rb_rect field_inner(const rb_node *n)
{
    return rr(n->ctl.x + 3, n->ctl.y + 2, n->ctl.w - 6, n->ctl.h - 4);
}

static int field_text_w(rbui *ui, rb_node *n, int upto)
{
    if (field_masked(n)) {
        int count = 0;
        size_t i = 0;
        while (i < (size_t)upto) {
            rbf_utf8_next(n->buf, (size_t)upto, &i);
            ++count;
        }
        return count * rbf_advance(ui->f_sys, 0x2022);
    }
    return rbf_width(ui->f_sys, n->buf, (size_t)upto);
}

int field_offset_x(rbui *ui, rb_node *n, int off)
{
    return field_text_w(ui, n, off);
}

void field_fix_scroll(rbui *ui, rb_node *n)
{
    int avail = n->ctl.w - 12;
    if (avail < 4) avail = 4;
    int cx = field_text_w(ui, n, n->caret), total = field_text_w(ui, n, n->len);
    if (cx - n->scroll_x > avail) n->scroll_x = cx - avail + avail / 3;
    if (cx - n->scroll_x < 0) n->scroll_x = cx - avail / 3;
    if (n->scroll_x > total - avail) n->scroll_x = total - avail;
    if (n->scroll_x < 0) n->scroll_x = 0;
}

int field_x_to_offset(rbui *ui, rb_node *n, int x)
{
    /* x relative to the text origin (scroll applied by the caller). */
    size_t i = 0;
    int w = 0;
    while (i < (size_t)n->len) {
        size_t j = i;
        uint32_t cp = rbf_utf8_next(n->buf, (size_t)n->len, &j);
        int a = field_masked(n) ? rbf_advance(ui->f_sys, 0x2022) : rbf_advance(ui->f_sys, cp);
        if (x < w + a / 2) break;
        w += a;
        i = j;
    }
    return (int)i;
}

/* ---- Controls ------------------------------------------------------------------ */

int button_width(const rbui *ui, const rb_node *b)
{
    int w = tx_width(ui->f_sys, 1, b->text, b->text ? strlen(b->text) : 0) + 24;
    if (b->width_hint > w) w = b->width_hint;
    return w < RB_BUTTON_MIN_W ? RB_BUTTON_MIN_W : w;
}

rb_rect radio_opt_rect(const rbui *ui, const rb_node *n, int i)
{
    const char *l = n->opts[i].label;
    int tw = tx_width(ui->f_sys, 1, l, strlen(l));
    int maxw = n->r.x + n->r.w - n->ctl.x;
    int w = 18 + tw + 4;
    if (w > maxw + 2) w = maxw + 2;
    return rr(n->ctl.x - 2, n->ctl.y + i * RB_CTL_ROW_H, w, RB_CTL_ROW_H);
}

rb_rect bevel_rect(const rb_node *n, int i)
{
    return rr(n->ctl.x + i * BEVEL_PITCH, n->ctl.y, BEVEL_W, BEVEL_W);
}

slider_parts slider_geom(const rbui *ui, const rb_node *n)
{
    slider_parts s;
    int x = n->r.x, y = n->r.y;
    s.label_x = x;
    int lw = n->text && n->text[0] ? tx_width(ui->f_sys, 1, n->text, strlen(n->text)) : 0;
    int tw = n->width_hint > 0 ? n->width_hint : SLIDER_TRACK_W;
    if (n->r.w > 0 && lw > 0) {
        int rest = (n->icon >= 0 ? 16 + 6 : 0) + tw + (n->icon2 >= 0 ? 7 + 16 : 0);
        int maxlw = n->r.w - rest - 9;
        if (maxlw < 0) maxlw = 0;
        if (lw > maxlw) lw = maxlw;
    }
    int at = lw ? x + lw + 9 : x;
    s.lo_x = n->icon >= 0 ? at : -1;
    if (n->icon >= 0) at += 16 + 6;
    s.track = rr(at, y + 4, tw, 7);
    s.hi_x = n->icon2 >= 0 ? at + tw + 7 : -1;
    double f = (n->val - n->vmin) / (n->vmax - n->vmin);
    if (f < 0) f = 0;
    if (f > 1) f = 1;
    s.thumb_x = at + (int)(f * (tw - SLIDER_THUMB_W) + 0.5);
    return s;
}

/* The value whose thumb would have its left edge at x, on the slider's steps. */
double slider_value_at(const rbui *ui, const rb_node *n, int x)
{
    slider_parts s = slider_geom(ui, n);
    int span = s.track.w - SLIDER_THUMB_W;
    double f = span > 0 ? (double)(x - s.track.x) / span : 0;
    if (f < 0) f = 0;
    if (f > 1) f = 1;
    double v = n->vmin + f * (n->vmax - n->vmin);
    v = n->vmin + (double)(long long)((v - n->vmin) / n->vstep + 0.5) * n->vstep;
    return v > n->vmax ? n->vmax : v;
}

int qr_module(const rb_node *n, int avail_w)
{
    int total = n->qr_w + 2 * n->quiet;
    if (total <= 0) return 1;
    if (n->module > 0) return n->module;
    int cap = avail_w < 200 ? avail_w : 200;
    int m = cap / total;
    if (m > 4) m = 4;
    return m < 1 ? 1 : m;
}

static int label_text_w(const rbui *ui, const rb_node *n)
{
    if (!n->text || !n->text[0]) return 0;
    return tx_width(ui->f_sys, 1, n->text, strlen(n->text));
}

static int popup_natural_w(const rbui *ui, const rb_node *n)
{
    int maxw = 0;
    for (int i = 0; i < n->nopts; ++i) {
        int tw = tx_width(ui->f_sys, 1, n->opts[i].label, strlen(n->opts[i].label));
        if (tw > maxw) maxw = tw;
    }
    return maxw + 41;
}

static int is_labelable(const rb_node *n)
{
    return n->type == N_FIELD || n->type == N_POPUP || n->type == N_RADIO;
}

/* ---- Layout ------------------------------------------------------------------ */

static int lay(rbui *ui, rb_node *n, int x, int y, int w);

static int gap_between(const rb_node *a, const rb_node *b)
{
    int ta = a->type, tb = b->type;
    if (ta == N_SPACER || tb == N_SPACER) return 0;
    if (ta == N_BAND || tb == N_BAND) return 12;
    if (tb == N_BUTTONS) return 16;
    if (ta == N_GROUP || tb == N_GROUP || ta == N_COLUMNS || tb == N_COLUMNS) return 12;
    if (ta == N_SEPARATOR || tb == N_SEPARATOR) return 10;
    if (ta == N_TEXT && tb == N_TEXT) return (a->style == ST_LARGE || b->style == ST_LARGE) ? 8 : 6;
    if (ta == N_CHECKBOX && tb == N_CHECKBOX) return 2;
    if ((ta == N_CHECKBOX || ta == N_RADIO) && (tb == N_CHECKBOX || tb == N_RADIO)) return 6;
    if ((ta == N_FIELD || ta == N_POPUP) && (tb == N_FIELD || tb == N_POPUP)) return 8;
    if (ta == N_TEXT && (tb == N_LIST || tb == N_FIELD || tb == N_PROGRESS)) return 8;
    return 10;
}

static int lay_stack(rbui *ui, rb_node **kids, int nkids, int x, int y, int w)
{
    /* Label columns: consecutive labelable controls (and checkboxes among
     * them) share the widest label. */
    int i = 0;
    while (i < nkids) {
        int j = i, widest = 0, any = 0;
        while (j < nkids && (is_labelable(kids[j]) || kids[j]->type == N_CHECKBOX)) {
            if (is_labelable(kids[j]) && kids[j]->text && kids[j]->text[0]) {
                int lw = label_text_w(ui, kids[j]);
                if (lw > widest) widest = lw;
                any = 1;
            }
            ++j;
        }
        if (widest > w / 2) widest = w / 2;
        for (int k = i; k < j; ++k) kids[k]->label_w = any ? widest : 0;
        /* Popups in one column share the widest natural width, as in Mac
         * dialogs where stacked popups line up on both edges. */
        int popw = 0;
        for (int k = i; k < j; ++k)
            if (kids[k]->type == N_POPUP && kids[k]->width_hint <= 0) {
                int pw = popup_natural_w(ui, kids[k]);
                if (pw > popw) popw = pw;
            }
        for (int k = i; k < j; ++k)
            if (kids[k]->type == N_POPUP) kids[k]->run_w = kids[k]->width_hint > 0 ? 0 : popw;
        if (j == i) {
            kids[i]->label_w = 0;
            ++i;
        } else {
            i = j;
        }
    }
    int cy = y;
    for (int k = 0; k < nkids; ++k) {
        if (k > 0) cy += gap_between(kids[k - 1], kids[k]);
        cy += lay(ui, kids[k], x, cy, w);
    }
    return cy - y;
}

static int lay_buttons(rbui *ui, rb_node *n, int x, int y, int w)
{
    int left_w = 0, right_w = 0, nleft = 0, nright = 0;
    for (int i = 0; i < n->nkids; ++i) {
        int bw = button_width(ui, n->kids[i]);
        if (n->kids[i]->align == AL_LEFT) {
            left_w += (nleft ? BUTTON_GAP : 0) + bw;
            nleft++;
        } else {
            right_w += (nright ? BUTTON_GAP : 0) + bw;
            nright++;
        }
    }
    int need = left_w + (nleft && nright ? BUTTON_GAP : 0) + right_w;
    if (need <= w) {
        int right = x + w, left = x;
        /* Left-aligned buttons from the left edge, the rest right-aligned. */
        for (int i = 0; i < n->nkids; ++i) {
            rb_node *b = n->kids[i];
            if (b->align != AL_LEFT) continue;
            int bw = button_width(ui, b);
            b->r = b->ctl = rr(left, y, bw, RB_BUTTON_H);
            left += bw + BUTTON_GAP;
        }
        for (int i = n->nkids - 1; i >= 0; --i) {
            rb_node *b = n->kids[i];
            if (b->align == AL_LEFT) continue;
            int bw = button_width(ui, b);
            right -= bw;
            b->r = b->ctl = rr(right, y, bw, RB_BUTTON_H);
            right -= BUTTON_GAP;
        }
        return RB_BUTTON_H;
    }
    /* Narrow screen fallback: wrap left and right button groups cleanly across rows. */
    int cy = y, row_gap = 8;
    if (nleft > 0) {
        int cx = x, row_cnt = 0;
        for (int i = 0; i < n->nkids; ++i) {
            rb_node *b = n->kids[i];
            if (b->align != AL_LEFT) continue;
            int bw = button_width(ui, b);
            if (row_cnt > 0 && cx + bw > x + w) {
                cy += RB_BUTTON_H + row_gap;
                cx = x;
                row_cnt = 0;
            }
            b->r = b->ctl = rr(cx, cy, bw, RB_BUTTON_H);
            cx += bw + BUTTON_GAP;
            row_cnt++;
        }
        if (nright > 0) cy += RB_BUTTON_H + row_gap;
    }
    if (nright > 0) {
        int i = 0;
        while (i < n->nkids) {
            while (i < n->nkids && n->kids[i]->align == AL_LEFT) i++;
            if (i >= n->nkids) break;
            int row_end = i, rw = 0, cnt = 0;
            for (int j = i; j < n->nkids; ++j) {
                if (n->kids[j]->align == AL_LEFT) continue;
                int bw = button_width(ui, n->kids[j]);
                int add = (cnt ? BUTTON_GAP : 0) + bw;
                if (cnt > 0 && rw + add > w) break;
                rw += add;
                row_end = j + 1;
                cnt++;
            }
            int cx = rw < w ? x + w - rw : x;
            for (int j = i; j < row_end; ++j) {
                if (n->kids[j]->align == AL_LEFT) continue;
                int bw = button_width(ui, n->kids[j]);
                n->kids[j]->r = n->kids[j]->ctl = rr(cx, cy, bw, RB_BUTTON_H);
                cx += bw + BUTTON_GAP;
            }
            i = row_end;
            int more = 0;
            for (int j = i; j < n->nkids; ++j)
                if (n->kids[j]->align != AL_LEFT) { more = 1; break; }
            if (more) cy += RB_BUTTON_H + row_gap;
        }
    }
    return cy + RB_BUTTON_H - y;
}

static int lay_columns(rbui *ui, rb_node *n, int x, int y, int w)
{
    if (n->nkids == 0) return 0;
    int avail = w - COLUMN_GAP * (n->nkids - 1), fixed = 0, flex = 0;
    for (int i = 0; i < n->nkids; ++i) {
        if (n->kids[i]->width_hint > 0) fixed += n->kids[i]->width_hint;
        else ++flex;
    }
    int share = flex ? (avail - fixed) / flex : 0, cx = x, h = 0;
    for (int i = 0; i < n->nkids; ++i) {
        rb_node *k = n->kids[i];
        int cw = k->width_hint > 0 ? k->width_hint : share;
        if (i == n->nkids - 1 && flex && k->width_hint <= 0) cw = x + w - cx;
        if (cw < 16) cw = 16;
        /* A labelled control placed directly in a column gets its own label column. */
        k->label_w = 0;
        if (is_labelable(k) && k->text && k->text[0]) {
            k->label_w = label_text_w(ui, k);
            if (k->label_w > cw / 2) k->label_w = cw / 2;
        }
        k->run_w = 0;
        int kh = lay(ui, k, cx, y, cw);
        if (kh > h) h = kh;
        cx += cw + COLUMN_GAP;
    }
    /* Group boxes side by side end on one line, as Monitors' two do. */
    for (int i = 0; i < n->nkids; ++i) {
        rb_node *k = n->kids[i];
        if (k->type == N_GROUP && k->ctl.h < h) k->ctl.h = k->r.h = h;
    }
    return h;
}

static int lay(rbui *ui, rb_node *n, int x, int y, int w)
{
    int h = 0, cx = x + (n->label_w ? n->label_w + LABEL_GAP : 0), cw = x + w - cx;
    switch (n->type) {
    case N_TEXT: {
        int tw = n->width_hint > 0 && n->width_hint < w ? n->width_hint : w;
        layout_text_lines(ui, n, tw);
        h = n->nlines * style_line_h(ui, n->style);
        n->ctl = rr(x, y, w, h);
        break;
    }
    case N_SEPARATOR:
        h = 2;
        n->ctl = rr(x, y, w, 2);
        break;
    case N_SPACER:
        h = n->height_hint;
        break;
    case N_ICON:
        h = n->icon_size;
        n->ctl = rr(x, y, n->icon_size, n->icon_size);
        break;
    case N_GROUP: {
        int top = n->text && n->text[0] ? 24 : 12;
        int inner = lay_stack(ui, n->kids, n->nkids, x + 12, y + top, w - 24);
        h = top + inner + 12;
        n->ctl = rr(x, y, w, h);
        break;
    }
    case N_STACK:
        h = lay_stack(ui, n->kids, n->nkids, x, y, w);
        break;
    case N_COLUMNS:
        h = lay_columns(ui, n, x, y, w);
        break;
    case N_BUTTONS:
        h = lay_buttons(ui, n, x, y, w);
        break;
    case N_FIELD: {
        int fw = n->width_hint > 0 && n->width_hint < cw ? n->width_hint : cw;
        n->ctl = rr(cx, y, fw, RB_FIELD_H);
        h = RB_FIELD_H;
        field_fix_scroll(ui, n);
        break;
    }
    case N_POPUP: {
        int pw = n->width_hint > 0 ? n->width_hint : n->run_w > 0 ? n->run_w : popup_natural_w(ui, n);
        if (pw < 72) pw = 72;
        if (pw > cw) pw = cw;
        n->ctl = rr(cx, y, pw, RB_POPUP_H);
        h = RB_POPUP_H;
        break;
    }
    case N_RADIO:
        n->ctl = rr(cx, y, cw, RB_CTL_ROW_H * (n->nopts ? n->nopts : 1));
        h = n->ctl.h;
        break;
    case N_CHECKBOX: {
        int tw = label_text_w(ui, n);
        int bw = 18 + tw + 4;
        if (bw > cw + 2) bw = cw + 2;
        n->ctl = rr(cx - 2, y, bw, RB_CTL_ROW_H);
        h = RB_CTL_ROW_H;
        break;
    }
    case N_LIST: {
        n->label_h = n->text && n->text[0] ? 20 : 0;
        int lw = n->width_hint > 0 && n->width_hint < w ? n->width_hint : w;
        if (n->icon_view) {
            /* Every row shown: 4 pixels above the first icon, 64 per row, the last name's box
             * and a little white below it. */
            int hh = n->header ? ICON_HEADER_H : 0;
            n->ctl = rr(x, y + n->label_h, lw, 2 + hh + icons_rows(n) * ICON_CELL_H - 8);
            h = n->label_h + n->ctl.h;
            n->scroll = 0;
            break;
        }
        n->ctl = rr(x, y + n->label_h, lw, n->rows * LIST_ROW_H + 2);
        h = n->label_h + n->ctl.h;
        list_clamp_scroll(n);
        break;
    }
    case N_PROGRESS: {
        int pw = n->width_hint > 0 && n->width_hint < w ? n->width_hint : w;
        n->ctl = rr(x + 1, y + 1, pw - 2, 12);
        h = 14;
        break;
    }
    case N_BAND: {
        /* Edge to edge in the window (Monitors' top, Sound's volume): out through the side
         * margins, and up through the top one when it is the window's first item. */
        rb_node *root = ui->doc.root;
        int bleed = n->parent == root && root->nkids > 0 && root->kids[0] == n ? RB_MARGIN : 0;
        rb_rect b = rr(x - RB_MARGIN, y - bleed, w + 2 * RB_MARGIN, n->height_hint);
        for (int i = 0; i < n->nkids; ++i) {
            rb_node *k = n->kids[i];
            int kx = k->ax > 0 ? k->ax : 0, ky = k->ay > 0 ? k->ay : 0;
            int kw = k->width_hint > 0 && k->type != N_SLIDER ? k->width_hint : b.w - kx - RB_MARGIN;
            k->label_w = 0;
            lay(ui, k, b.x + kx, b.y + ky, kw);
        }
        n->ctl = n->r = b;
        return b.h - bleed;
    }
    case N_BEVELS:
        h = BEVEL_H;
        n->ctl = rr(x, y, n->nopts ? n->nopts * BEVEL_PITCH - (BEVEL_PITCH - BEVEL_W) : 0, BEVEL_H);
        break;
    case N_SLIDER: {
        n->r = rr(x, y, w, SLIDER_H);
        slider_parts s = slider_geom(ui, n);
        n->ctl = rr(s.track.x, y, s.track.w, SLIDER_H);   /* the thumb's travel: what a press hits */
        h = SLIDER_H;
        break;
    }
    case N_FACTS:
        h = n->nopts * FACTS_ROW_H;
        n->ctl = rr(x, y, w, h);
        break;
    case N_METERS:
        /* The list frame, then the rows with a white line between each two. */
        h = n->nopts ? 2 + n->nopts * METER_ROW_H - 1 : 0;
        n->ctl = rr(x, y, w, h);
        break;
    case N_QR: {
        int m = qr_module(n, w);
        int total = (n->qr_w + 2 * n->quiet) * m, th = (n->qr_h + 2 * n->quiet) * m;
        int qx = x + (w - total) / 2;
        if (qx < x) qx = x;
        n->ctl = rr(qx, y, total, th);
        h = th;
        break;
    }
    default:
        break;
    }
    n->r = rr(x, y, w, h);
    return h;
}

typedef struct { int scrolled; } mark_ctx;

static int mark_fn(rb_node *n, void *ctx)
{
    n->scrolled = ((mark_ctx *)ctx)->scrolled;
    return 0;
}

static int content_layout(rbui *ui, int x, int y, int w)
{
    rb_node *root = ui->doc.root;
    int n = root->nkids - (ui->pinned ? 1 : 0) - (ui->pinned_band ? 1 : 0);
    if (n < 0) n = 0;
    return lay_stack(ui, root->kids, n, x, y, w);
}

void layout_doc(rbui *ui)
{
    ui->pinned = ui->pinned_band = NULL;
    ui->scrollable = 0;
    ui->vbar = ui->icon_r = rr(0, 0, 0, 0);
    if (!ui->have_doc || !ui->doc.root) return;
    rb_doc *d = &ui->doc;
    rb_node *root = d->root;
    if (root->nkids > 0 && root->kids[root->nkids - 1]->type == N_BUTTONS) ui->pinned = root->kids[root->nkids - 1];
    int last = root->nkids - 1 - (ui->pinned ? 1 : 0);
    if (last > 0 && root->kids[last]->type == N_BAND && root->kids[last]->pin) ui->pinned_band = root->kids[last];

    int titled = d->kind == 0 || (d->title && d->title[0]);
    int is_alert = d->kind == 1;
    int W = d->width;
    if (W > ui->cw - 16) W = ui->cw - 16;
    if (W < 160) W = 160;
    int extra = pt_window_extra_h(titled);
    int cw = W - 12;                                   /* content width */
    int tx = RB_MARGIN, tw = cw - 2 * RB_MARGIN;       /* item column (content relative) */
    if (is_alert && d->alert_icon >= 0) {
        tx = RB_MARGIN + 2 + 32 + ALERT_ICON_GAP;
        tw = cw - tx - RB_MARGIN;
    }
    int buttons_h = ui->pinned ? lay_buttons(ui, ui->pinned, 0, 0, cw - 2 * RB_MARGIN) : 0;
    int pinned_h = ui->pinned ? 16 + buttons_h : 0;
    int band_h = ui->pinned_band ? ui->pinned_band->height_hint + (ui->pinned ? 0 : RB_MARGIN) : 0;

    /* First pass at full width to measure. */
    int items_h = content_layout(ui, 0, 0, tw);
    int body_h = items_h;
    if (is_alert && d->alert_icon >= 0 && body_h < 32) body_h = 32;
    int content_h = RB_MARGIN + body_h + band_h + pinned_h + RB_MARGIN;
    if (content_h < d->min_h) content_h = d->min_h;
    int H = extra + content_h;
    int maxH = ui->ch - 16;
    if (maxH < extra + 80) maxH = extra + 80;
    if (H > maxH) {
        ui->scrollable = 1;
        H = maxH;
        tw -= RB_SCROLLBAR_W;
        content_layout(ui, 0, 0, tw);   /* re-wrap at the narrower width */
    }
    int fx = (ui->cw - W) / 2, fy = (ui->ch - H) / 2;
    if (fy < 0) fy = 0;
    ui->frame = rr(fx, fy, W, H);
    ui->content = pt_window_content(ui->frame, titled);
    rb_rect c = ui->content;

    int gap_above_buttons = (ui->scrollable && !ui->pinned_band) ? 12 : 16;
    int view_bottom = c.y + c.h - (ui->pinned ? RB_MARGIN + buttons_h + gap_above_buttons : 0) - band_h;
    ui->view = rr(c.x, c.y, c.w, view_bottom - c.y);
    if (!ui->scrollable) ui->view = c;

    /* Final pass at absolute coordinates. */
    int top = c.y + RB_MARGIN;
    if (is_alert && d->alert_icon >= 0) ui->icon_r = rr(c.x + RB_MARGIN + 2, top, 32, 32);
    ui->content_h = RB_MARGIN + content_layout(ui, c.x + tx, top, tw) + RB_MARGIN;
    if (ui->scrollable) {
        ui->vbar = rr(c.x + c.w - RB_SCROLLBAR_W, c.y, RB_SCROLLBAR_W, ui->view.h);
        int max = ui->content_h - ui->view.h;
        if (ui->scroll > max) ui->scroll = max;
        if (ui->scroll < 0) ui->scroll = 0;
    } else {
        ui->scroll = 0;
    }
    mark_ctx in = {1}, out = {0};
    node_walk(root, mark_fn, &in);
    root->scrolled = 0;
    if (ui->pinned) {
        int by = c.y + c.h - RB_MARGIN - buttons_h;
        int bx = c.x + RB_MARGIN, bw = c.w - 2 * RB_MARGIN;
        lay(ui, ui->pinned, bx, by, bw);
        node_walk(ui->pinned, mark_fn, &out);
    }
    if (ui->pinned_band) {
        /* Sound's Main Volume: across the window's bottom, just above the buttons (or its edge). */
        int by = ui->pinned ? c.y + c.h - RB_MARGIN - buttons_h - 16 - ui->pinned_band->height_hint
                            : c.y + c.h - ui->pinned_band->height_hint;
        lay(ui, ui->pinned_band, c.x + RB_MARGIN, by, c.w - 2 * RB_MARGIN);
        node_walk(ui->pinned_band, mark_fn, &out);
    }
    if (ui->menu) menu_layout(ui);
}

/* ---- Popup menu geometry --------------------------------------------------- */

void menu_layout(rbui *ui)
{
    rb_node *n = ui->menu;
    if (!n || n->nopts == 0) return;
    rb_rect b = ui_node_screen(ui, n, n->ctl);
    int maxw = 0;
    for (int i = 0; i < n->nopts; ++i) {
        int tw = tx_width(ui->f_sys, 1, n->opts[i].label, strlen(n->opts[i].label));
        if (tw > maxw) maxw = tw;
    }
    int w = maxw + 20 + 18;
    if (w < b.w + 11) w = b.w + 11;
    if (w > ui->cw - 8) w = ui->cw - 8;
    int total = n->nopts * RB_MENU_ITEM_H + 6;
    int maxh = ui->ch - 8;
    int rows = n->nopts;
    ui->menu_arrows = 0;
    if (total > maxh) {
        rows = (maxh - 6) / RB_MENU_ITEM_H;
        if (rows < 3) rows = 3;
        total = rows * RB_MENU_ITEM_H + 6;
        ui->menu_arrows = 1;
    }
    int sel = n->sel >= 0 ? n->sel : 0;
    /* Items' text lines up with the popup's own text (Mac popup menus open
     * over the control with the current item in place). */
    int x = b.x - 11;
    if (x + w > ui->cw - 4) x = ui->cw - 4 - w;
    if (x < 4) x = 4;
    if (!ui->menu_arrows) {
        int item_y = b.y + (b.h - RB_MENU_ITEM_H) / 2;
        int y = item_y - 3 - sel * RB_MENU_ITEM_H;
        if (y + total > ui->ch - 4) y = ui->ch - 4 - total;
        if (y < 4) y = 4;
        ui->menu_r = rr(x, y, w, total);
        ui->menu_first = 0;
        ui->menu_rows = rows;
    } else {
        int y = (ui->ch - total) / 2;
        ui->menu_r = rr(x, y, w, total);
        ui->menu_rows = rows;
        if (ui->menu_first > n->nopts - (rows - 2)) ui->menu_first = n->nopts - (rows - 2);
        if (ui->menu_first < 0) ui->menu_first = 0;
    }
}
