/*
 * rbui.h - RetroBoot classic panel UI library (Mac OS 8/9 "Platinum" look)
 *
 * rbui lays out, renders and handles input for the declarative UI documents
 * described in docs/ARCHITECTURE.md section 4.2. The presenter owns the
 * display and input devices; rbui works purely on a logical XRGB8888 canvas
 * (the presenter scales it by an integer factor, section 4.5).
 *
 * Threading: an rbui object is not thread safe; use it from one thread.
 * Time: rbui never reads a clock. Call rbui_tick() with a monotonic time in
 * milliseconds regularly (every frame, or at least every 50 ms while the
 * panel is shown) and before delivering input, so caret blink, button flash,
 * autoscroll, double-click and type-ahead timing work.
 */
#ifndef RBUI_H
#define RBUI_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct rbui rbui;

/* Receives one event as a JSON object string, valid only during the call:
 * {"event":"ui_action","doc":ID,"id":BUTTON_ID,"values":{...}} or
 * {"event":"ui_change","doc":ID,"id":WIDGET_ID,"value":V} (ARCHITECTURE §4).
 * Changes caused by a new document never emit ui_change; only user input does. */
typedef void (*rbui_emit_fn)(void *ctx, const char *event_json);

enum {
    RBUI_KEY_NONE = 0, RBUI_KEY_TAB, RBUI_KEY_RETURN, RBUI_KEY_ESCAPE, RBUI_KEY_BACKSPACE, RBUI_KEY_DELETE,
    RBUI_KEY_LEFT, RBUI_KEY_RIGHT, RBUI_KEY_UP, RBUI_KEY_DOWN, RBUI_KEY_HOME, RBUI_KEY_END,
    RBUI_KEY_PAGEUP, RBUI_KEY_PAGEDOWN, RBUI_KEY_SPACE, RBUI_KEY_KP_ENTER
};
enum { RBUI_MOD_SHIFT = 1, RBUI_MOD_CTRL = 2, RBUI_MOD_OPTION = 4, RBUI_MOD_COMMAND = 8 };
enum { RBUI_BG_CHECKERBOARD = 0, RBUI_BG_BLACK = 1, RBUI_BG_IMAGE_DIMMED = 2,
       /* bg_image as it is (the presenter's DPI-aware checkerboard) */
       RBUI_BG_IMAGE = 3 };

/* The first rbui_new() loads the image's Platinum chrome, then optionally uses
 * fonts, icons and controls from /var/lib/retroboot/assets/platinum.
 * RBUI_ASSETS selects an explicit bundle; RBUI_ASSETS="" uses compiled-in assets.
 * rbui_load_assets() loads from a directory explicitly (before rbui_new);
 * returns the number of fonts, icons and sprites loaded. */
int rbui_load_assets(const char *dir);
rbui *rbui_new(void);
void rbui_free(rbui *ui);
void rbui_set_emitter(rbui *ui, rbui_emit_fn fn, void *ctx);

/* Replace the document (JSON per §4.2). Returns 0 on success, or -1 with a
 * message in err (the previous document stays shown). Widget state (focus,
 * field text and caret, list selection and scroll, checkbox/radio/popup
 * values, window scroll) is preserved by widget id unless the doc has
 * "reset":true. A value hostd changes between two documents always wins;
 * a value hostd repeats unchanged keeps what the user did since. NULL or ""
 * clears the document. */
int rbui_set_doc(rbui *ui, const char *json, size_t len, char *err, size_t errlen);

/* Logical canvas size (e.g. 960x540). Re-centres the window; full damage. */
void rbui_resize(rbui *ui, int logical_w, int logical_h);

/* Advance animations (caret blink, button flash, autoscroll, indeterminate
 * progress). Returns nonzero if a redraw is needed or an animation is
 * running: the value is then the number of milliseconds (1..100) until rbui
 * wants the next tick. 0 means nothing is pending (tick again on input, or
 * at a relaxed interval). Check rbui_damage() for what to redraw. */
int rbui_tick(rbui *ui, uint64_t now_ms);

/* Pointer in logical coordinates; bit0 of buttons = primary button down. */
void rbui_pointer(rbui *ui, int x, int y, unsigned buttons);

/* Keyboard. key = RBUI_KEY_* or 0 for plain text; utf8 = text the key
 * produced (may be NULL). Only down (and auto-repeat) events act. */
void rbui_key(rbui *ui, int key, const char *utf8, int down, unsigned mods);

/* Render the whole logical frame: background, the window/alert, any open
 * popup menu, then the classic arrow cursor if show_cursor. bg_image is the
 * frozen guest frame for RBUI_BG_IMAGE_DIMMED, or the background itself for
 * RBUI_BG_IMAGE (same logical size), else NULL. xrgb must hold logical_h
 * rows of stride_px pixels. Clears damage. */
void rbui_render(rbui *ui, uint32_t *xrgb, int stride_px, int background,
                 const uint32_t *bg_image, int bg_stride_px, int show_cursor);

/* Bounding rectangle of everything that changed since the last render
 * (including cursor moves); 0 if nothing. Changing the background type or
 * image is the caller's to track. */
int rbui_damage(rbui *ui, int *x, int *y, int *w, int *h);

/* §4.4: white where (x+y) is even, black otherwise; phase anchored at
 * logical (0,0) whatever rectangle is drawn. */
void rbui_draw_checkerboard(uint32_t *xrgb, int stride_px, int x, int y, int w, int h);
/* Pattern phase (0/1) of the checkerboard: the presenter shifts it when its
 * output is rotated so the pattern matches the raw boot checkerboard. */
void rbui_set_checker_phase(int phase);

void rbui_cursor_pos(const rbui *ui, int *x, int *y);

/* Built-in documents (static JSON strings), or NULL for unknown names:
 *   "hostd-lost": "RetroBoot needs to restart." stop alert, buttons
 *                 "shutdown" (Shut Down) and "restart" (Restart, default). */
const char *rbui_builtin_doc(const char *name);

/* ---- Additive extensions ------------------------------------------------ */

/* Move keyboard focus to the widget (or button) with this id; NULL clears
 * focus. Returns 0, or -1 if no such focusable widget. */
int rbui_focus(rbui *ui, const char *id);

/* Scroll wheel: dy steps (positive = down, toward later rows). Scrolls the
 * list under the pointer, else the window content when it overflows, or an
 * open popup menu that is taller than the canvas. */
void rbui_wheel(rbui *ui, int dy);

/* Id of the current document ("" if none) and of the focused widget (NULL). */
const char *rbui_doc_id(const rbui *ui);
const char *rbui_focus_id(const rbui *ui);

/* Malloc'd JSON describing the current layout for tests and automation:
 * {"doc":ID,"window":[x,y,w,h],"content":[...],"widgets":[{"id":..,
 * "type":..,"rect":[x,y,w,h],"focused":bool,...}]}. Caller frees(). */
char *rbui_describe(const rbui *ui);

/* Write a logical XRGB frame as an 8-bit RGB PNG (stb_image_write, kept
 * private to rbui so callers may also use the vendored header themselves).
 * Returns 0 on success. */
int rbui_write_png(const char *path, const uint32_t *xrgb, int w, int h, int stride_px);

/* Button feedback (feedback.c): the indicator rb-presenter shows for the
 * brightness, volume and mute keys, laid out like the window Mac OS 9 draws
 * for its own volume keys (title, quiet icon, level bar, loud icon), at
 * logical scale.  rbui_feedback_size gives its fixed size; rbui_draw_feedback
 * fills an opaque w x h XRGB rectangle (stride in pixels).  level is 0..1,
 * shown in RBUI_FEEDBACK_STEPS steps (any level above 0 shows at least one);
 * the bar is empty while muted (volume and mute). */
enum { RBUI_FEEDBACK_VOLUME = 0, RBUI_FEEDBACK_MUTE = 1, RBUI_FEEDBACK_BRIGHTNESS = 2 };
#define RBUI_FEEDBACK_STEPS 16
void rbui_feedback_size(int *w, int *h);
int rbui_feedback_steps(double level);
void rbui_draw_feedback(uint32_t *xrgb, int stride_px, int kind, double level, int muted);

#ifdef __cplusplus
}
#endif

#endif /* RBUI_H */
