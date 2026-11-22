#pragma once
#include <stdbool.h>

struct rb_opts {
    const char *display_socket;
    const char *control_socket;
    const char *remote_socket; /* web Screen page (remote.h); NULL or "" disables it */
    const char *socket_group;
    const char *device;
    const char *connector;
    const char *initial_scene;
    const char *record_guest; /* lab: frame log of the guest's frames as they arrive (framelog.h) */
    bool no_vt;
    bool no_input;
    bool no_handoff;
    bool restore_vt;
};

int rb_presenter_main(const struct rb_opts *opts);
