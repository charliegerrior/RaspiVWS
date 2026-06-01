#!/usr/bin/env python3
"""Set the LED mode on a Logitech C920 / C920 PRO via UVC extension unit.

Usage: c920_led.py <device_number> <mode>
  mode 0 = off, 1 = always on, 2 = blink, 3 = auto (on when streaming)

Targets Unit 11 (UVC_GUID_LOGITECH_PERIPHERAL), Selector 9
(XU_PERIPHERALCONTROL_LED), which carries the LED mode in bits 0-2 of
byte 1 of the 5-byte control payload.  This XU is present on C920 PRO
firmware (product 0x08e5) and other V3 Logitech cameras.
"""

import fcntl, struct, ctypes, os, sys

def IOWR(t, nr, size):
    return (3 << 30) | (t << 8) | nr | (size << 16)

_FMT = '=BBBxH2xQ'
_UVCIOC_CTRL_QUERY = IOWR(ord('u'), 0x21, struct.calcsize(_FMT))
_UVC_GET_CUR = 0x81
_UVC_SET_CUR = 0x01
_XU_UNIT = 11
_XU_SEL  = 9
_CTRL_LEN = 5

def set_led(device_number, mode):
    path = f'/dev/video{device_number}'
    fd = os.open(path, os.O_RDWR)
    try:
        buf = ctypes.create_string_buffer(_CTRL_LEN)
        q = struct.pack(_FMT, _XU_UNIT, _XU_SEL, _UVC_GET_CUR, _CTRL_LEN, ctypes.addressof(buf))
        fcntl.ioctl(fd, _UVCIOC_CTRL_QUERY, bytearray(q))
        payload = bytearray(buf.raw)
        payload[1] = (payload[1] & ~0x07) | (mode & 0x07)
        buf2 = ctypes.create_string_buffer(bytes(payload))
        q = struct.pack(_FMT, _XU_UNIT, _XU_SEL, _UVC_SET_CUR, _CTRL_LEN, ctypes.addressof(buf2))
        fcntl.ioctl(fd, _UVCIOC_CTRL_QUERY, bytearray(q))
    finally:
        os.close(fd)

if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(f'Usage: {sys.argv[0]} <device_number> <mode 0-3>', file=sys.stderr)
        sys.exit(1)
    set_led(int(sys.argv[1]), int(sys.argv[2]))
