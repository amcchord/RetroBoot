/* Screen rotation (ARCHITECTURE 4.5 "Rotation").
 *
 * Every output's content rotation (rb_output.rot, clockwise, content ->
 * scanout) is its panel orientation (panel_rot: the connector's "panel
 * orientation" property or a DMI quirk) plus, for the primary output only,
 * the user rotation: display.rotation, or in "auto" the accelerometer's
 * orientation, chosen by hostd and sent as display_config "rotation". The
 * user rotation turns the picture the way the device is held; the frame
 * (and so the Mac screen) becomes portrait at 90 and 270. Mirrors keep their
 * own panel orientation, so an external display shows the picture upright.
 *
 * GRUB passes the user rotation it drew its background for as
 * retroboot.rotation= (rb-bootdisplay), so the splash and the presenter
 * start with the same frame. */
#pragma once
#include <stdbool.h>

/* 0, 90, 180 or 270 for any multiple of 90 (negative too); -1 otherwise. */
int rb_rotation_norm(int degrees);
/* (a + b) mod 360, for rotations that are multiples of 90. */
int rb_rotation_add(int a, int b);
static inline bool rb_rotation_swaps(int rot) { return rot == 90 || rot == 270; }

/* "retroboot.rotation=0|90|180|270" on a kernel command line (the last one
 * wins); 0 when absent or invalid. */
int rb_cmdline_rotation(const char *cmdline);
/* rb_cmdline_rotation of a file (NULL = /proc/cmdline); 0 when unreadable. */
int rb_boot_rotation(const char *path);

/* HyperPixel 4 rectangular's ILI9806E-SPI panel driver does not export a DRM
 * panel orientation. Read only that known panel's DT rotation property as a
 * fallback. NULL root = /proc/device-tree; -1 when absent/unknown/invalid. */
int rb_hyperpixel_panel_rotation(const char *root);
