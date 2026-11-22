#!/usr/bin/env python3
"""Create a temporary uinput device and emit KEY_POWER / lid switch events
(lab tool, root): exercises rb-presenter's power_button and lid events on a
VM without those devices.

  uinput_events.py power        # press + release KEY_POWER
  uinput_events.py lid-close    # SW_LID=1
  uinput_events.py lid-open     # SW_LID=0 (no event if already open)
  uinput_events.py lid-cycle    # close then open on one device
  uinput_events.py lid-hold S   # close, hold S seconds, open
  uinput_events.py key CODE [N] # press + release evdev key CODE N times
                                # (224/225 brightness down/up, 113 mute, 114/115 volume)
"""
import fcntl
import os
import struct
import sys
import time

UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_SWBIT = 0x40045564, 0x40045565, 0x4004556d
UI_DEV_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY = 0x405c5503, 0x5501, 0x5502
EV_SYN, EV_KEY, EV_SW = 0, 1, 5
KEY_POWER, SW_LID = 116, 0
EXTRA_KEYS = (113, 114, 115, 224, 225)


def emit(fd, typ, code, val):
    os.write(fd, struct.pack('llHHi', 0, 0, typ, code, val))


def main(what, args=()):
    fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    fcntl.ioctl(fd, UI_SET_KEYBIT, KEY_POWER)
    for k in EXTRA_KEYS:
        fcntl.ioctl(fd, UI_SET_KEYBIT, k)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_SW)
    fcntl.ioctl(fd, UI_SET_SWBIT, SW_LID)
    setup = struct.pack('HHHH80sI', 0x06, 0x1234, 0x5678, 1, b'rb-test power+lid', 0)
    fcntl.ioctl(fd, UI_DEV_SETUP, setup)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    time.sleep(1.0)  # let udev/libinput pick the device up
    if what == 'power':
        emit(fd, EV_KEY, KEY_POWER, 1)
        emit(fd, EV_SYN, 0, 0)
        time.sleep(0.05)
        emit(fd, EV_KEY, KEY_POWER, 0)
        emit(fd, EV_SYN, 0, 0)
    elif what in ('lid-close', 'lid-open'):
        emit(fd, EV_SW, SW_LID, 1 if what == 'lid-close' else 0)
        emit(fd, EV_SYN, 0, 0)
    elif what == 'key':
        code, n = int(args[0]), int(args[1]) if len(args) > 1 else 1
        for _ in range(n):
            emit(fd, EV_KEY, code, 1)
            emit(fd, EV_SYN, 0, 0)
            time.sleep(0.05)
            emit(fd, EV_KEY, code, 0)
            emit(fd, EV_SYN, 0, 0)
            time.sleep(0.3)
    elif what == 'lid-hold':
        emit(fd, EV_SW, SW_LID, 1)
        emit(fd, EV_SYN, 0, 0)
        time.sleep(float(args[0]) if args else 5.0)
        emit(fd, EV_SW, SW_LID, 0)
        emit(fd, EV_SYN, 0, 0)
    elif what == 'lid-cycle':
        for v in (1, 0):
            emit(fd, EV_SW, SW_LID, v)
            emit(fd, EV_SYN, 0, 0)
            time.sleep(0.3)
    time.sleep(0.5)
    fcntl.ioctl(fd, UI_DEV_DESTROY)
    os.close(fd)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'power', sys.argv[2:])
