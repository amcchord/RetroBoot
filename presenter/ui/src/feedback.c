/* feedback.c - button feedback: the presenter's on-screen indicator for the
 * brightness, volume and mute keys.
 *
 * Mac OS 9's own feedback for its volume keys is a small plain window at the
 * bottom of the screen: a title ("Volume", "Mute On", "Mute Off"), a quiet
 * speaker, a level bar and a loud speaker.  RetroBoot draws the same layout
 * when Mac OS cannot (brightness, which Mac OS 9.2.1 changes without feedback
 * on this machine, and every key while Mac OS is not running), with rbui's
 * own Platinum frame, font, progress bar and original icons. */
#include <string.h>

#include "rbui.h"
#include "rbui_internal.h"

enum {
    FB_W = 212, FB_H = 58,
    FB_TITLE_BASE = 18,
    FB_ICON_Y = 30, FB_ICON_X = 14,
    FB_BAR_H = 12,
};

void rbui_feedback_size(int *w, int *h)
{
    if (w) *w = FB_W;
    if (h) *h = FB_H;
}

int rbui_feedback_steps(double level)
{
    if (!(level > 0.0)) return 0;   /* also NaN */
    if (level >= 1.0) return RBUI_FEEDBACK_STEPS;
    int n = (int)(level * RBUI_FEEDBACK_STEPS + 0.5);
    return n < 1 ? 1 : n;            /* any sound or light shows one step */
}

void rbui_draw_feedback(uint32_t *xrgb, int stride_px, int kind, double level, int muted)
{
    rb_gfx g = {xrgb, stride_px, FB_W, FB_H, rr(0, 0, FB_W, FB_H), 0, 0};
    const rbf_font *font = rbf_get("system12");

    /* Plain Platinum box: black outline, raised bevel, dialog face. */
    g_fill(&g, 0, 0, FB_W, FB_H, C_DE);
    g_frame(&g, rr(0, 0, FB_W, FB_H), C_BLACK);
    g_hline(&g, 1, 1, FB_W - 2, C_WHITE);
    g_vline(&g, 1, 1, FB_H - 2, C_WHITE);
    g_hline(&g, 1, FB_H - 2, FB_W - 2, C_B5);
    g_vline(&g, FB_W - 2, 1, FB_H - 2, C_B5);
    g_pixel(&g, FB_W - 2, 1, C_DE);
    g_pixel(&g, 1, FB_H - 2, C_DE);

    const char *title;
    const char *left, *right;
    switch (kind) {
    case RBUI_FEEDBACK_BRIGHTNESS:
        title = "Brightness";
        left = "sun_low";
        right = "sun_high";
        break;
    case RBUI_FEEDBACK_MUTE:
        title = muted ? "Mute On" : "Mute Off";
        left = muted ? "speaker_mute" : "speaker_low";
        right = "sound";
        break;
    default:
        title = "Volume";
        left = muted ? "speaker_mute" : "speaker_low";
        right = "sound";
        break;
    }
    if (font) {
        int tw = tx_width(font, 1, title, strlen(title));
        tx_draw(&g, font, 1, (FB_W - tw) / 2, FB_TITLE_BASE, title, strlen(title), C_BLACK);
    }
    icon_draw_named(&g, left, 16, FB_ICON_X, FB_ICON_Y, 0);
    icon_draw_named(&g, right, 16, FB_W - FB_ICON_X - 16, FB_ICON_Y, 0);

    /* The level in RBUI_FEEDBACK_STEPS steps, empty while muted. */
    int bx = FB_ICON_X + 16 + 8, bw = FB_W - 2 * bx;
    int steps = (kind != RBUI_FEEDBACK_BRIGHTNESS && muted) ? 0 : rbui_feedback_steps(level);
    pt_progress(&g, rr(bx, FB_ICON_Y + (16 - FB_BAR_H) / 2, bw, FB_BAR_H),
                (double)steps / RBUI_FEEDBACK_STEPS, 0, 0, 0);
}
