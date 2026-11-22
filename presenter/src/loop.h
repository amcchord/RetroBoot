/* Minimal epoll event loop with one-shot timers (timerfd) and an idle hook
 * that runs once after every dispatch batch (used to compose and present). */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <sys/epoll.h>

struct rb_loop;
struct rb_watch;
struct rb_timer;

typedef void (*rb_watch_fn)(void *data, int fd, uint32_t events);
typedef void (*rb_timer_fn)(void *data);
typedef void (*rb_idle_fn)(void *data);

struct rb_loop *rb_loop_new(void);
void rb_loop_free(struct rb_loop *loop);

struct rb_watch *rb_loop_watch(struct rb_loop *loop, int fd, uint32_t events, rb_watch_fn fn, void *data);
int rb_watch_set_events(struct rb_watch *w, uint32_t events);
/* Removes the watch; the fd itself is not closed. Safe during dispatch. */
void rb_watch_remove(struct rb_watch *w);

struct rb_timer *rb_timer_new(struct rb_loop *loop, rb_timer_fn fn, void *data);
/* One-shot timer in milliseconds; 0 disarms. */
void rb_timer_arm(struct rb_timer *t, uint64_t ms);
bool rb_timer_armed(const struct rb_timer *t);
void rb_timer_free(struct rb_timer *t);

void rb_loop_set_idle(struct rb_loop *loop, rb_idle_fn fn, void *data);
/* Waits up to timeout_ms (-1 = forever), dispatches, then runs the idle hook. */
int rb_loop_dispatch(struct rb_loop *loop, int timeout_ms);
void rb_loop_quit(struct rb_loop *loop, int code);
bool rb_loop_running(const struct rb_loop *loop);
int rb_loop_exit_code(const struct rb_loop *loop);
