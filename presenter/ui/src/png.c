/* png.c - rbui_write_png() on top of the vendored stb_image_write
 * (implementation in stb_impl.c). */
#include <stdlib.h>

#include "rbui.h"
#include "stb_image_write.h"

int rbui_write_png(const char *path, const uint32_t *xrgb, int w, int h, int stride_px)
{
    if (!path || !xrgb || w <= 0 || h <= 0 || stride_px < w) return -1;
    unsigned char *rgb = malloc((size_t)w * (size_t)h * 3);
    if (!rgb) return -1;
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x) {
            uint32_t p = xrgb[(size_t)y * (size_t)stride_px + (size_t)x];
            unsigned char *o = rgb + ((size_t)y * (size_t)w + (size_t)x) * 3;
            o[0] = (unsigned char)(p >> 16);
            o[1] = (unsigned char)(p >> 8);
            o[2] = (unsigned char)p;
        }
    int ok = stbi_write_png(path, w, h, 3, rgb, w * 3);
    free(rgb);
    return ok ? 0 : -1;
}
