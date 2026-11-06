#!/usr/bin/env python3
"""Inject a single-finger touch stroke through a temporary uinput touchscreen.

Usage: uinput-touch.py W H x1,y1 [x2,y2 ...] [--hold SECONDS]

Coordinates are logical screen pixels; W/H are the logical screen size used to
convert them to the virtual panel's 0..4095 raw range.  The device is created,
the stroke is emitted, and the device is removed again.
"""
import ctypes
import fcntl
import os
import sys
import time

UINPUT_MAX_NAME_SIZE = 80
ABS_CNT = 64
EV_SYN = 0
EV_KEY = 1
EV_ABS = 3
SYN_REPORT = 0
BTN_TOUCH = 0x14A
ABS_X = 0x00
ABS_Y = 0x01
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39
INPUT_PROP_DIRECT = 1
BUS_USB = 0x03

UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
UI_SET_PROPBIT = 0x4004556E
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502


class InputEvent(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long),
                ("tv_usec", ctypes.c_long),
                ("type", ctypes.c_uint16),
                ("code", ctypes.c_uint16),
                ("value", ctypes.c_int32)]


class InputId(ctypes.Structure):
    _fields_ = [("bustype", ctypes.c_uint16),
                ("vendor", ctypes.c_uint16),
                ("product", ctypes.c_uint16),
                ("version", ctypes.c_uint16)]


class UinputUserDev(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char * UINPUT_MAX_NAME_SIZE),
                ("id", InputId),
                ("ff_effects_max", ctypes.c_uint32),
                ("absmax", ctypes.c_int32 * ABS_CNT),
                ("absmin", ctypes.c_int32 * ABS_CNT),
                ("absfuzz", ctypes.c_int32 * ABS_CNT),
                ("absflat", ctypes.c_int32 * ABS_CNT)]


def emit(fd, etype, code, value):
    event = InputEvent(0, 0, etype, code, value)
    os.write(fd, bytes(event))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 3:
        print(__doc__.splitlines()[2], file=sys.stderr)
        return 2
    width, height = float(args[0]), float(args[1])
    points = []
    for part in args[2:]:
        x, y = part.split(",")
        points.append((float(x), float(y)))
    hold = 0.3
    if "--hold" in sys.argv:
        hold = float(sys.argv[sys.argv.index("--hold") + 1])

    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    try:
        for ev in (EV_KEY, EV_ABS):
            fcntl.ioctl(fd, UI_SET_EVBIT, ev)
        fcntl.ioctl(fd, UI_SET_KEYBIT, BTN_TOUCH)
        for ab in (ABS_X, ABS_Y, ABS_MT_SLOT, ABS_MT_POSITION_X,
                   ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID):
            fcntl.ioctl(fd, UI_SET_ABSBIT, ab)
        fcntl.ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)

        dev = UinputUserDev()
        dev.name = b"opencode-virtual-touch"
        dev.id = InputId(BUS_USB, 0, 0, 1)
        for ab in (ABS_X, ABS_Y, ABS_MT_POSITION_X, ABS_MT_POSITION_Y):
            dev.absmin[ab] = 0
            dev.absmax[ab] = 4095
        dev.absmin[ABS_MT_SLOT] = 0
        dev.absmax[ABS_MT_SLOT] = 9
        dev.absmin[ABS_MT_TRACKING_ID] = -1
        dev.absmax[ABS_MT_TRACKING_ID] = 65535
        os.write(fd, bytes(dev))
        fcntl.ioctl(fd, UI_DEV_CREATE)
        time.sleep(1.5)

        def raw(point):
            x, y = point
            return (max(0, min(4095, int(round(x * 4095.0 / width)))),
                    max(0, min(4095, int(round(y * 4095.0 / height)))))

        rx, ry = raw(points[0])
        emit(fd, EV_ABS, ABS_MT_SLOT, 0)
        emit(fd, EV_ABS, ABS_MT_TRACKING_ID, 1)
        emit(fd, EV_ABS, ABS_MT_POSITION_X, rx)
        emit(fd, EV_ABS, ABS_MT_POSITION_Y, ry)
        emit(fd, EV_ABS, ABS_X, rx)
        emit(fd, EV_ABS, ABS_Y, ry)
        emit(fd, EV_KEY, BTN_TOUCH, 1)
        emit(fd, EV_SYN, SYN_REPORT, 0)
        time.sleep(hold)

        for point in points[1:]:
            rx, ry = raw(point)
            emit(fd, EV_ABS, ABS_MT_POSITION_X, rx)
            emit(fd, EV_ABS, ABS_MT_POSITION_Y, ry)
            emit(fd, EV_ABS, ABS_X, rx)
            emit(fd, EV_ABS, ABS_Y, ry)
            emit(fd, EV_SYN, SYN_REPORT, 0)
            time.sleep(0.08)
        time.sleep(hold)

        emit(fd, EV_ABS, ABS_MT_TRACKING_ID, -1)
        emit(fd, EV_KEY, BTN_TOUCH, 0)
        emit(fd, EV_SYN, SYN_REPORT, 0)
        time.sleep(0.4)
    finally:
        try:
            fcntl.ioctl(fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())