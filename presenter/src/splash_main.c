/* rb-splash: the initramfs build of the splash. Same sources as
 * `rb-presenter --splash`, linked without libinput/xkbcommon/classic-ui so
 * the initramfs only needs libdrm and libudev. */
#include <string.h>

#include "splash.h"

int main(int argc, char **argv)
{
    if (argc > 2 && !strcmp(argv[1], "--splash-chroot"))
        return rb_splash_send_chroot(argv[2]);
    return rb_splash_main(argc, argv);
}
