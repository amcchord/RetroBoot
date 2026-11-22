/*
 * rb-unzstd: decompress a zstd stream from standard input to standard output.
 *
 * Shipped beside gemini-install.sh as a static aarch64 binary, because Gemian (Debian 9)
 * may have no zstd at all. Built by image/gemini/scripts/build-unzstd.sh against Debian
 * trixie's libzstd.a with the builder's aarch64 cross compiler. Handles concatenated
 * frames; any error (corrupt or truncated input, write failure) exits 1 with a message.
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <zstd.h>

static int write_all(int fd, const char *p, size_t n)
{
    while (n > 0) {
        ssize_t w = write(fd, p, n);
        if (w < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        p += w;
        n -= (size_t)w;
    }
    return 0;
}

int main(int argc, char **argv)
{
    if (argc > 1 && (strcmp(argv[1], "--version") == 0 || strcmp(argv[1], "-V") == 0)) {
        printf("rb-unzstd (libzstd %s)\n", ZSTD_versionString());
        return 0;
    }
    if (argc > 1) {
        fprintf(stderr, "usage: rb-unzstd < IN.zst > OUT\n");
        return 2;
    }
    size_t in_cap = ZSTD_DStreamInSize(), out_cap = ZSTD_DStreamOutSize();
    char *in = malloc(in_cap), *out = malloc(out_cap);
    ZSTD_DCtx *dctx = ZSTD_createDCtx();
    if (!in || !out || !dctx) {
        fprintf(stderr, "rb-unzstd: out of memory\n");
        return 1;
    }
    /* Allow the long-distance windows zstd --long=31 may produce. */
    ZSTD_DCtx_setParameter(dctx, ZSTD_d_windowLogMax, 31);
    size_t last = 0;
    int any = 0;
    for (;;) {
        ssize_t r = read(0, in, in_cap);
        if (r < 0) {
            if (errno == EINTR)
                continue;
            fprintf(stderr, "rb-unzstd: read: %s\n", strerror(errno));
            return 1;
        }
        if (r == 0)
            break;
        any = 1;
        ZSTD_inBuffer ib = { in, (size_t)r, 0 };
        while (ib.pos < ib.size) {
            ZSTD_outBuffer ob = { out, out_cap, 0 };
            last = ZSTD_decompressStream(dctx, &ob, &ib);
            if (ZSTD_isError(last)) {
                fprintf(stderr, "rb-unzstd: %s\n", ZSTD_getErrorName(last));
                return 1;
            }
            if (write_all(1, out, ob.pos) < 0) {
                fprintf(stderr, "rb-unzstd: write: %s\n", strerror(errno));
                return 1;
            }
        }
    }
    /* Flush what the decoder still holds for a frame whose input ended exactly. */
    for (;;) {
        ZSTD_inBuffer ib = { in, 0, 0 };
        ZSTD_outBuffer ob = { out, out_cap, 0 };
        if (last == 0)
            break;
        last = ZSTD_decompressStream(dctx, &ob, &ib);
        if (ZSTD_isError(last)) {
            fprintf(stderr, "rb-unzstd: %s\n", ZSTD_getErrorName(last));
            return 1;
        }
        if (write_all(1, out, ob.pos) < 0) {
            fprintf(stderr, "rb-unzstd: write: %s\n", strerror(errno));
            return 1;
        }
        if (ob.pos == 0)
            break;
    }
    if (!any || last != 0) {
        fprintf(stderr, "rb-unzstd: %s\n", any ? "truncated input (the last frame is incomplete)" : "no input");
        return 1;
    }
    ZSTD_freeDCtx(dctx);
    return 0;
}
