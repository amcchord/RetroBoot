#pragma once
#include <stdio.h>
#include <stdlib.h>

static int rbt_failures, rbt_checks;

#define CHECK(cond)                                                                                                    \
    do {                                                                                                               \
        rbt_checks++;                                                                                                  \
        if (!(cond)) {                                                                                                 \
            rbt_failures++;                                                                                            \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond);                                   \
        }                                                                                                              \
    } while (0)

#define CHECK_EQ(a, b)                                                                                                 \
    do {                                                                                                               \
        long long _a = (long long)(a), _b = (long long)(b);                                                            \
        rbt_checks++;                                                                                                  \
        if (_a != _b) {                                                                                                \
            rbt_failures++;                                                                                            \
            fprintf(stderr, "%s:%d: CHECK_EQ failed: %s == %lld, %s == %lld\n", __FILE__, __LINE__, #a, _a, #b, _b);   \
        }                                                                                                              \
    } while (0)

#define TEST_DONE()                                                                                                    \
    do {                                                                                                               \
        fprintf(stderr, "%d checks, %d failures\n", rbt_checks, rbt_failures);                                         \
        return rbt_failures ? 1 : 0;                                                                                   \
    } while (0)

static unsigned rbt_seed = 12345;
static inline unsigned rbt_rand(void)
{
    rbt_seed = rbt_seed * 1103515245u + 12345u;
    return rbt_seed >> 8;
}
