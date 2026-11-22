#include "loop.h"

#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <sys/timerfd.h>
#include <unistd.h>

struct rb_watch {
    struct rb_loop *loop;
    int fd;
    rb_watch_fn fn;
    void *data;
    bool dead;
    struct rb_watch *next_dead;
    struct rb_watch *prev, *next; /* live list */
};

struct rb_timer {
    struct rb_watch *watch;
    int fd;
    rb_timer_fn fn;
    void *data;
    bool armed;
    struct rb_timer *next;
};

struct rb_loop {
    int epfd;
    bool running;
    int exit_code;
    rb_idle_fn idle;
    void *idle_data;
    struct rb_watch *dead;
    struct rb_watch *live;
    struct rb_timer *timers;
};

struct rb_loop *rb_loop_new(void)
{
    struct rb_loop *loop = calloc(1, sizeof *loop);
    if (!loop)
        return NULL;
    loop->epfd = epoll_create1(EPOLL_CLOEXEC);
    if (loop->epfd < 0) {
        free(loop);
        return NULL;
    }
    loop->running = true;
    return loop;
}

static void reap(struct rb_loop *loop)
{
    while (loop->dead) {
        struct rb_watch *w = loop->dead;
        loop->dead = w->next_dead;
        free(w);
    }
}

void rb_loop_free(struct rb_loop *loop)
{
    if (!loop)
        return;
    while (loop->timers) {
        struct rb_timer *t = loop->timers;
        loop->timers = t->next;
        close(t->fd);
        free(t);
    }
    while (loop->live) {
        struct rb_watch *w = loop->live;
        loop->live = w->next;
        free(w);
    }
    reap(loop);
    close(loop->epfd);
    free(loop);
}

struct rb_watch *rb_loop_watch(struct rb_loop *loop, int fd, uint32_t events, rb_watch_fn fn, void *data)
{
    struct rb_watch *w = calloc(1, sizeof *w);
    if (!w)
        return NULL;
    w->loop = loop;
    w->fd = fd;
    w->fn = fn;
    w->data = data;
    struct epoll_event ev = {.events = events, .data.ptr = w};
    if (epoll_ctl(loop->epfd, EPOLL_CTL_ADD, fd, &ev) < 0) {
        free(w);
        return NULL;
    }
    w->next = loop->live;
    if (loop->live)
        loop->live->prev = w;
    loop->live = w;
    return w;
}

int rb_watch_set_events(struct rb_watch *w, uint32_t events)
{
    struct epoll_event ev = {.events = events, .data.ptr = w};
    return epoll_ctl(w->loop->epfd, EPOLL_CTL_MOD, w->fd, &ev);
}

void rb_watch_remove(struct rb_watch *w)
{
    if (!w || w->dead)
        return;
    epoll_ctl(w->loop->epfd, EPOLL_CTL_DEL, w->fd, NULL);
    if (w->prev)
        w->prev->next = w->next;
    else
        w->loop->live = w->next;
    if (w->next)
        w->next->prev = w->prev;
    w->dead = true;
    w->next_dead = w->loop->dead;
    w->loop->dead = w;
}

static void timer_ready(void *data, int fd, uint32_t events)
{
    (void)events;
    struct rb_timer *t = data;
    uint64_t expirations;
    if (read(fd, &expirations, sizeof expirations) < 0 && errno == EAGAIN)
        return;
    t->armed = false;
    t->fn(t->data);
}

struct rb_timer *rb_timer_new(struct rb_loop *loop, rb_timer_fn fn, void *data)
{
    struct rb_timer *t = calloc(1, sizeof *t);
    if (!t)
        return NULL;
    t->fd = timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK | TFD_CLOEXEC);
    if (t->fd < 0) {
        free(t);
        return NULL;
    }
    t->fn = fn;
    t->data = data;
    t->watch = rb_loop_watch(loop, t->fd, EPOLLIN, timer_ready, t);
    if (!t->watch) {
        close(t->fd);
        free(t);
        return NULL;
    }
    t->next = loop->timers;
    loop->timers = t;
    return t;
}

void rb_timer_arm(struct rb_timer *t, uint64_t ms)
{
    struct itimerspec its;
    memset(&its, 0, sizeof its);
    if (ms) {
        its.it_value.tv_sec = (time_t)(ms / 1000u);
        its.it_value.tv_nsec = (long)(ms % 1000u) * 1000000L;
    }
    timerfd_settime(t->fd, 0, &its, NULL);
    t->armed = ms != 0;
}

bool rb_timer_armed(const struct rb_timer *t) { return t && t->armed; }

void rb_timer_free(struct rb_timer *t)
{
    if (!t)
        return;
    struct rb_loop *loop = t->watch->loop;
    for (struct rb_timer **pp = &loop->timers; *pp; pp = &(*pp)->next)
        if (*pp == t) {
            *pp = t->next;
            break;
        }
    rb_watch_remove(t->watch);
    close(t->fd);
    free(t);
}

void rb_loop_set_idle(struct rb_loop *loop, rb_idle_fn fn, void *data)
{
    loop->idle = fn;
    loop->idle_data = data;
}

int rb_loop_dispatch(struct rb_loop *loop, int timeout_ms)
{
    struct epoll_event events[32];
    int n = epoll_wait(loop->epfd, events, 32, timeout_ms);
    if (n < 0) {
        if (errno != EINTR)
            return -1;
        n = 0;
    }
    for (int i = 0; i < n; i++) {
        struct rb_watch *w = events[i].data.ptr;
        if (w->dead)
            continue;
        w->fn(w->data, w->fd, events[i].events);
    }
    if (loop->idle)
        loop->idle(loop->idle_data);
    reap(loop);
    return n;
}

void rb_loop_quit(struct rb_loop *loop, int code)
{
    loop->running = false;
    loop->exit_code = code;
}

bool rb_loop_running(const struct rb_loop *loop) { return loop->running; }
int rb_loop_exit_code(const struct rb_loop *loop) { return loop->exit_code; }
