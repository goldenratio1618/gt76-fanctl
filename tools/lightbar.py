#!/usr/bin/env python3
"""Set the GT76's light bars (front bar and side bars) from Linux.

The bars are driven by a SteelSeries USB lighting controller (USB 1038:1130), not the EC. It speaks
the protocol documented for the MSI GE66 Raider's light bar
(https://github.com/Negraldi/ge66-lighting, docs/PROTOCOL.md). Zones measured on this machine:

    zones 0-24  front bar, left to right (each about 4 % of the bar)
    zone  25    both side bars (one zone; they always show the same color)
    zones 26-34 accepted by the controller but not connected to any LEDs

Colors are live only: this tool never sends the controller's save-to-flash (09 00) or reset (F1 00)
commands, so settings last until the next power cycle and Windows' own settings are untouched.
Needs read/write access to the controller's hidraw node (udev/70-gt76-lightbar.rules grants it to
the logged-in user).

Usage:
  lightbar.py color RRGGBB [--sides RRGGBB]     one color (sides default to the same color)
  lightbar.py gradient RRGGBB RRGGBB [...]      front bar fades left to right through the colors;
              [--sides RRGGBB]                  sides default to the first color
  lightbar.py off
  lightbar.py version                           print the controller's firmware version
Add --brightness 0-255 (default 255) to color/gradient.
"""
import argparse
import fcntl
import glob
import os
import select
import sys
import time

USB_ID = "HID_ID=0003:00001038:00001130"
FRONT_ZONES = 25
SIDE_ZONE = 25
REPORT = 64          # output report size
FEATURE = 524        # feature report size
FORBIDDEN = {(0x09, 0x00), (0xF1, 0x00)}  # save to flash, reset controller


def hidiocsfeature(n):
    return (3 << 30) | (n << 16) | (ord("H") << 8) | 0x06


def find_device():
    """hidraw node of the controller's vendor interface (usage page 0xFFC0)."""
    for h in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(f"{h}/device/uevent") as f:
                uevent = f.read()
            with open(f"{h}/device/report_descriptor", "rb") as f:
                desc = f.read()
        except OSError:
            continue
        if USB_ID in uevent and desc[:3] == bytes([0x06, 0xC0, 0xFF]):
            return "/dev/" + os.path.basename(h)
    return None


def device_identity(path):
    """sysfs path of the HID device behind a hidraw node; its instance number changes on re-enumeration."""
    return os.path.realpath(f"/sys/class/hidraw/{os.path.basename(path)}/device")


def parse_color(s):
    s = s.lstrip("#")
    if len(s) != 6:
        raise argparse.ArgumentTypeError(f"color must be RRGGBB, got {s!r}")
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        raise argparse.ArgumentTypeError(f"color must be hex RRGGBB, got {s!r}") from None


def gradient(stops, n=FRONT_ZONES):
    """n colors fading linearly through the given stops."""
    if len(stops) == 1:
        return [stops[0]] * n
    out = []
    for i in range(n):
        pos = i / (n - 1) * (len(stops) - 1)
        k = min(int(pos), len(stops) - 2)
        f = pos - k
        out.append(tuple(round(a + (b - a) * f) for a, b in zip(stops[k], stops[k + 1])))
    return out


def zone_payload(front, side):
    """Feature report body for a direct write of zones 0-25: 0C 00 N 00, then N x (zone, R, G, B)."""
    zones = list(front) + [side]
    body = [0x0C, 0x00, len(zones), 0x00]
    for i, rgb in enumerate(zones):
        body += [i, *rgb]
    return bytes(body)


class Controller:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR)

    def close(self):
        os.close(self.fd)

    def out(self, *b):
        assert tuple(b[:2]) not in FORBIDDEN
        os.write(self.fd, bytes([0, *b]) + bytes(REPORT - len(b)))
        time.sleep(0.01)

    def feature(self, body):
        assert tuple(body[:2]) not in FORBIDDEN
        buf = bytearray([0, *body]) + bytearray(FEATURE - len(body))
        fcntl.ioctl(self.fd, hidiocsfeature(len(buf)), buf)
        time.sleep(0.01)

    def version(self):
        while select.select([self.fd], [], [], 0)[0]:  # drop stale input
            os.read(self.fd, REPORT)
        self.out(0x10, 0x00)
        if not select.select([self.fd], [], [], 1.0)[0]:
            return None
        r = os.read(self.fd, REPORT)
        return f"{r[1]}.{r[0]}"

    def show(self, front, side, brightness=255):
        self.out(0x51, 0x00, 0xFF, 0xFF, 0x99)  # color scale used by MSI's software
        self.out(0x0C, 0x00, brightness)
        self.feature(zone_payload(front, side))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("color"); c.add_argument("color", type=parse_color)
    g = sub.add_parser("gradient"); g.add_argument("colors", type=parse_color, nargs="+")
    for p in (c, g):
        p.add_argument("--sides", type=parse_color)
        p.add_argument("--brightness", type=int, default=255, choices=range(256), metavar="0-255")
    sub.add_parser("off")
    sub.add_parser("version")
    a = ap.parse_args(argv)

    path = find_device()
    if not path:
        sys.exit("light-bar controller (USB 1038:1130) not found")
    try:
        ctl = Controller(path)
    except PermissionError:
        sys.exit(f"no access to {path}; install udev/70-gt76-lightbar.rules (install.sh does this) and log in again")
    try:
        if a.cmd == "version":
            print(ctl.version() or "no reply")
        elif a.cmd == "off":
            ctl.show([(0, 0, 0)] * FRONT_ZONES, (0, 0, 0))
        elif a.cmd == "color":
            ctl.show(gradient([a.color]), a.sides or a.color, a.brightness)
        else:
            ctl.show(gradient(a.colors), a.sides or a.colors[0], a.brightness)
    finally:
        ctl.close()


if __name__ == "__main__":
    main()
