#!/usr/bin/env python3
"""Tests for tools/lightbar.py's report building (no hardware touched)."""
import argparse
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("lightbar", os.path.join(HERE, "..", "tools", "lightbar.py"))
lb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lb)


class LightbarTest(unittest.TestCase):
    def test_parse_color(self):
        self.assertEqual(lb.parse_color("#ff8000"), (255, 128, 0))
        for bad in ("fff", "gg0000", "1234567"):
            with self.assertRaises(argparse.ArgumentTypeError):
                lb.parse_color(bad)

    def test_gradient_endpoints_and_length(self):
        g = lb.gradient([(255, 0, 0), (0, 0, 255)])
        self.assertEqual(len(g), lb.FRONT_ZONES)
        self.assertEqual((g[0], g[-1]), ((255, 0, 0), (0, 0, 255)))
        g3 = lb.gradient([(0, 0, 0), (200, 0, 0), (0, 0, 0)])
        self.assertEqual(g3[12], (200, 0, 0))  # middle stop lands on the middle zone

    def test_zone_payload(self):
        p = lb.zone_payload([(1, 2, 3)] * lb.FRONT_ZONES, (9, 8, 7))
        self.assertEqual(p[:4], bytes([0x0C, 0x00, 26, 0x00]))
        self.assertEqual(p[4:8], bytes([0, 1, 2, 3]))
        self.assertEqual(p[-4:], bytes([lb.SIDE_ZONE, 9, 8, 7]))
        self.assertLessEqual(len(p), lb.FEATURE)

    def test_flash_save_and_reset_refused(self):
        ctl = lb.Controller.__new__(lb.Controller)
        ctl.fd = -1
        for cmd in ((0x09, 0x00), (0xF1, 0x00)):
            with self.assertRaises(AssertionError):
                ctl.out(*cmd)


if __name__ == "__main__":
    unittest.main()
