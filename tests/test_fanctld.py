#!/usr/bin/env python3
"""Tests for fanctld against a fake /sys tree and an in-memory EC (no hardware touched).

Run: python3 -m unittest discover -s tests
"""
import importlib.util
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NCPU = 4

# Factory EC image: firmware id, thresholds, curves, auto mode, idle temps and fans.
EC = bytearray(256)
EC[0xA0:0xAC] = b"17H3EMS1.102"
EC[0x6A:0x70] = bytes([50, 61, 68, 75, 80, 90])
EC[0x72:0x79] = bytes([20, 40, 50, 60, 70, 80, 100])
EC[0x82:0x88] = bytes([50, 61, 69, 74, 80, 88])
EC[0x8A:0x91] = bytes([20, 30, 45, 60, 70, 80, 100])
EC[0xF4] = 0x0C
EC[0x68], EC[0x80] = 45, 47
EC[0xCC:0xCE] = (390).to_bytes(2, "big")
EC[0xCA:0xCC] = (398).to_bytes(2, "big")


def put(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def ec_dump_text(ec):
    out = "|      | _0 _1 _2 _3 _4 _5 _6 _7 _8 _9 _a _b _c _d _e _f\n|------+------------------------------------------------\n"
    for i in range(16):
        out += f"| {hex(i) if i else '0'}_ |" + "".join(f" {b:02x}" for b in ec[i * 16:i * 16 + 16]) + "  |................|\n"
    return out


class FanctldTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.environ["FANCTL_SYSROOT"] = self.root
        os.environ["FANCTL_STATE_DIR"] = f"{self.root}/etc"
        os.environ["FANCTL_SOCKET"] = f"{self.root}/fanctl.sock"
        for c in range(NCPU):
            d = f"{self.root}/sys/devices/system/cpu/cpu{c}/cpufreq"
            os.makedirs(d)
            for name, v in (("cpuinfo_max_freq", 5300000), ("scaling_max_freq", 5300000), ("scaling_cur_freq", 3800000)):
                put(f"{d}/{name}", str(v))
        rapl = f"{self.root}/sys/class/powercap/intel-rapl:0"
        os.makedirs(rapl)
        for name, v in (("constraint_0_power_limit_uw", 170000000), ("constraint_0_time_window_us", 55967744),
                        ("constraint_1_power_limit_uw", 300000000), ("energy_uj", 1000000),
                        ("max_energy_range_uj", 262143328850)):
            put(f"{rapl}/{name}", str(v))
        self.ecdir = f"{self.root}/sys/devices/platform/msi-ec/debug"
        os.makedirs(self.ecdir)
        self.ec = bytearray(EC)
        self.sync_ec()

        spec = importlib.util.spec_from_file_location("fanctld", os.path.join(HERE, "..", "fanctld.py"))
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.writes = []

        def fake_ec_set(addr, value):
            self.writes.append((addr, value))
            self.ec[addr] = value
            self.sync_ec()
        self.m.ec_set = fake_ec_set
        self.c = self.m.Controller()

    def tearDown(self):
        shutil.rmtree(self.root)

    def sync_ec(self):
        put(f"{self.ecdir}/ec_dump", ec_dump_text(self.ec))

    def sysint(self, rel):
        with open(f"{self.root}/sys/{rel}") as f:
            return int(f.read())

    def test_validate_rejects_bad_settings(self):
        good = dict(self.m.INITIAL_DEFAULTS)
        for bad in ({"max_clock_mhz": 3950}, {"max_clock_mhz": 900}, {"max_clock_mhz": 5400},
                    {"pl1_w": 200, "pl2_w": 150}, {"pl1_window_s": 0}, {"fan_mode": "turbo"},
                    {"cpu_curve": [20] * 6}, {"gpu_curve": [20] * 6 + [101]}):
            with self.assertRaises(ValueError, msg=bad):
                self.c.validate(dict(good, **bad))
        self.assertEqual(self.c.validate(good), good)

    def test_defaults_applied_to_hardware(self):
        self.c.set_live(self.c.defaults)
        self.assertEqual(self.c.errors, [])
        for cpu in range(NCPU):
            self.assertEqual(self.sysint(f"devices/system/cpu/cpu{cpu}/cpufreq/scaling_max_freq"), 3900000)
        self.assertEqual(self.sysint("class/powercap/intel-rapl:0/constraint_0_power_limit_uw"), 135000000)
        self.assertEqual(self.sysint("class/powercap/intel-rapl:0/constraint_0_time_window_us"), 10000000)
        self.assertEqual(self.writes, [])  # factory fan settings already on the EC: nothing written

    def test_custom_curve_written_and_drift_reapplied(self):
        curve = [70, 70, 70, 70, 70, 80, 100]
        self.c.handle({"cmd": "apply", "settings": {"fan_mode": "advanced", "cpu_curve": curve}})
        self.assertEqual(list(self.ec[0x72:0x79]), curve)
        self.assertEqual(self.ec[0xF4], 0x8C)
        self.ec[0x72:0x79] = EC[0x72:0x79]  # EC resets (e.g. resume from sleep)
        self.ec[0xF4] = 0x0C
        self.sync_ec()
        put(f"{self.root}/sys/devices/system/cpu/cpu2/cpufreq/scaling_max_freq", "5300000")
        self.c.enforce_once()
        self.assertEqual(list(self.ec[0x72:0x79]), curve)
        self.assertEqual(self.ec[0xF4], 0x8C)
        self.assertEqual(self.sysint("devices/system/cpu/cpu2/cpufreq/scaling_max_freq"), 3900000)

    def test_thermal_failsafe_reverts_fans(self):
        self.c.handle({"cmd": "apply", "settings": {"fan_mode": "advanced", "cpu_curve": [30] * 7}})
        self.ec[0x68] = 96
        self.sync_ec()
        for _ in range(self.m.FAILSAFE_SAMPLES):
            self.c.enforce_once()
        st = self.c.handle({"cmd": "status"})
        self.assertIn("failsafe", st["failsafe"].lower())
        self.assertEqual(self.ec[0xF4], 0x0C)
        self.assertEqual(list(self.ec[0x72:0x79]), self.m.FACTORY["cpu_curve"])
        self.assertEqual(st["live"]["max_clock_mhz"], 3900)  # clock untouched
        self.c.handle({"cmd": "apply", "settings": {"fan_mode": "auto"}})  # a new apply clears it
        self.assertIsNone(self.c.failsafe)

    def test_wrong_firmware_blocks_ec_writes(self):
        self.ec[0xA0:0xAC] = b"17H1EMS1.104"
        self.sync_ec()
        self.c.handle({"cmd": "apply", "settings": {"fan_mode": "advanced"}})
        self.assertEqual(self.writes, [])
        self.assertTrue(any("firmware" in e for e in self.c.errors))

    def test_restore_and_save_defaults(self):
        self.c.handle({"cmd": "apply", "settings": {"max_clock_mhz": 4100, "pl1_w": 160}})
        self.c.handle({"cmd": "save_defaults"})
        with open(f"{self.root}/etc/defaults.json") as f:
            self.assertEqual(json.load(f)["max_clock_mhz"], 4100)
        self.assertEqual(self.m.Controller().defaults["max_clock_mhz"], 4100)
        st = self.c.handle({"cmd": "restore", "which": "factory"})
        self.assertEqual(st["live"], self.m.FACTORY)
        self.assertEqual(self.sysint("devices/system/cpu/cpu0/cpufreq/scaling_max_freq"), 5300000)
        self.assertEqual(self.c.handle({"cmd": "restore", "which": "defaults"})["live"]["max_clock_mhz"], 4100)

    def test_status_readings(self):
        hw = self.c.handle({"cmd": "status"})["hw"]
        self.assertEqual((hw["cpu_temp"], hw["gpu_temp"]), (45, 47))
        self.assertEqual(hw["cpu_rpm"], 478000 // 390)
        self.assertEqual(hw["fan_mode"], "auto")
        self.assertEqual(hw["cpu_thresholds"], [50, 61, 68, 75, 80, 90])

    def test_socket_roundtrip(self):
        self.m.SOCKET_GROUP = ""
        threading.Thread(target=self.m.serve, args=(self.c,), daemon=True).start()
        for _ in range(50):
            if os.path.exists(os.environ["FANCTL_SOCKET"]):
                break
            time.sleep(0.02)
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(os.environ["FANCTL_SOCKET"])
            s.sendall(b'{"cmd": "apply", "settings": {"max_clock_mhz": 3950}}\n{"cmd": "status"}\n')
            f = s.makefile()
            bad, good = json.loads(f.readline()), json.loads(f.readline())
        self.assertFalse(bad["ok"])
        self.assertIn("multiple of 100", bad["error"])
        self.assertTrue(good["ok"])


if __name__ == "__main__":
    unittest.main()
