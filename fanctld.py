#!/usr/bin/env python3
"""gt76-fanctld: root service owning the fan curves, CPU clock cap and power limits of an
MSI GT76 Titan DT 10SGS (EC firmware 17H3EMS1.102).

Clients (the GUI) talk to it over a Unix socket, one JSON object per line:
  {"cmd": "status"}
  {"cmd": "apply", "settings": {...partial settings...}}
  {"cmd": "restore", "which": "factory" | "defaults"}
  {"cmd": "save_defaults"}
Every hardware write is validated here. A background loop re-applies the requested
settings if anything drifts (EC reset, resume from sleep, other tools), and reverts the
fans to the factory curve if temperatures get dangerous while a custom curve is active.

EC access goes through the msi-ec driver's debug interface
(/sys/devices/platform/msi-ec/debug), which works under Secure Boot when msi-ec is a
signed module loaded with debug=1.
"""
import copy
import grp
import json
import logging
import os
import socketserver
import subprocess
import threading
import time

SYSROOT = os.environ.get("FANCTL_SYSROOT", "")  # tests point this at a fake /sys tree
SOCKET_PATH = os.environ.get("FANCTL_SOCKET", "/run/gt76-fanctl.sock")
STATE_DIR = os.environ.get("FANCTL_STATE_DIR", "/etc/gt76-fanctl")
SOCKET_GROUP = os.environ.get("FANCTL_GROUP", "")
ENFORCE_INTERVAL = 2.0

MSI_EC = SYSROOT + "/sys/devices/platform/msi-ec/debug"
CPU_DIR = SYSROOT + "/sys/devices/system/cpu"
RAPL = SYSROOT + "/sys/class/powercap/intel-rapl:0"

# --- EC layout (verified on 17H3EMS1.102) -------------------------------------------
EC_FW_ADDR, EC_FW = 0xA0, b"17H3EMS1.102"
CPU_TEMP, GPU_TEMP = 0x68, 0x80            # degrees C
CPU_LEVEL, GPU_LEVEL = 0x71, 0x89          # current fan-curve target, percent
CPU_THRESH, GPU_THRESH = 0x6A, 0x82        # 6 band boundaries each, degrees C
CPU_CURVE, GPU_CURVE = 0x72, 0x8A          # 7 band speeds each, percent
EXPECTED_CPU_THRESH = [50, 61, 68, 75, 80, 90]
EXPECTED_GPU_THRESH = [50, 61, 69, 74, 80, 88]
FAN_MODE = 0xF4                            # EC follows the curves only in advanced mode
FAN_MODES = {"auto": 0x0C, "advanced": 0x8C}
CPU_TACH, GPU_TACH = 0xCC, 0xCA            # 16-bit big-endian tach period
RPM_CONST = 478000                         # rpm = RPM_CONST / period

# --- Settings ----------------------------------------------------------------------
# Values read from this machine before any changes were made.
FACTORY = {
    "max_clock_mhz": 5300,
    "pl1_w": 170, "pl1_window_s": 56, "pl2_w": 300,
    "fan_mode": "auto",
    "cpu_curve": [20, 40, 50, 60, 70, 80, 100],
    "gpu_curve": [20, 30, 45, 60, 70, 80, 100],
}
# Starting defaults: 3.9 GHz cap, PL1 a little above the ~119 W full AVX load draws
# there (a noise safety net that normally never binds), PL2 far above real draw
# (a PL2 near actual draw makes the clock oscillate), factory fan curve.
INITIAL_DEFAULTS = dict(FACTORY, max_clock_mhz=3900, pl1_w=135, pl1_window_s=10)

FAILSAFE_CPU_C, FAILSAFE_GPU_C, FAILSAFE_SAMPLES = 95, 90, 3

log = logging.getLogger("gt76-fanctld")


# --- Low-level I/O -------------------------------------------------------------------
def read_text(path):
    with open(path) as f:
        return f.read().strip()


def read_int(path):
    try:
        return int(read_text(path))
    except (OSError, ValueError):
        return None


def write_text(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def cpu_dirs():
    return sorted((d for d in os.listdir(CPU_DIR) if d.startswith("cpu") and d[3:].isdigit()),
                  key=lambda d: int(d[3:]))


def ec_available():
    if not os.path.isdir(MSI_EC) and not SYSROOT:
        subprocess.run(["modprobe", "msi-ec", "debug=1"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.isdir(MSI_EC)


def read_ec():
    rows = read_text(f"{MSI_EC}/ec_dump").splitlines()[2:18]
    data = bytes(int(b, 16) for r in rows for b in r.split("|")[2].split())
    if len(data) != 256:
        raise OSError("unexpected ec_dump format")
    return data


def ec_set(addr, value):
    write_text(f"{MSI_EC}/ec_set", f"{addr:02x}={value:02x}")


def ec_guard(ec):
    """Refuse EC writes unless this is the EC layout this service was written for."""
    if ec[EC_FW_ADDR:EC_FW_ADDR + 12] != EC_FW:
        raise RuntimeError(f"EC firmware {ec[EC_FW_ADDR:EC_FW_ADDR + 12]!r} is not {EC_FW!r}")
    if list(ec[CPU_THRESH:CPU_THRESH + 6]) != EXPECTED_CPU_THRESH or \
            list(ec[GPU_THRESH:GPU_THRESH + 6]) != EXPECTED_GPU_THRESH:
        raise RuntimeError("EC fan-curve thresholds are not where expected")


def rpm(ec, addr):
    period = ec[addr] << 8 | ec[addr + 1]
    return RPM_CONST // period if period else 0


# --- Controller ----------------------------------------------------------------------
class Controller:
    def __init__(self):
        self.lock = threading.RLock()
        self.defaults = self._load_defaults()
        self.live = copy.deepcopy(self.defaults)
        self.failsafe = None      # message when the thermal failsafe has overridden the fans
        self.errors = []          # problems from the last apply
        self.hot_samples = 0
        self.last_energy = None   # (energy_uj, time) for package power

    # Settings files
    def _load_defaults(self):
        try:
            with open(f"{STATE_DIR}/defaults.json") as f:
                return self.validate(dict(INITIAL_DEFAULTS, **json.load(f)))
        except FileNotFoundError:
            return copy.deepcopy(INITIAL_DEFAULTS)
        except (ValueError, OSError) as e:
            log.error("ignoring unreadable defaults.json: %s", e)
            return copy.deepcopy(INITIAL_DEFAULTS)

    def _save_defaults(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = f"{STATE_DIR}/defaults.json.tmp"
        with open(tmp, "w") as f:
            json.dump(self.defaults, f, indent=2)
        os.replace(tmp, f"{STATE_DIR}/defaults.json")

    def cpuinfo_max_mhz(self):
        return (read_int(f"{CPU_DIR}/cpu0/cpufreq/cpuinfo_max_freq") or 5300000) // 1000

    def validate(self, s):
        out = {}
        mhz = int(s["max_clock_mhz"])
        if mhz % 100 or not 1000 <= mhz <= self.cpuinfo_max_mhz():
            raise ValueError(f"max_clock_mhz must be a multiple of 100 in 1000..{self.cpuinfo_max_mhz()}")
        out["max_clock_mhz"] = mhz
        out["pl1_w"], out["pl2_w"] = int(s["pl1_w"]), int(s["pl2_w"])
        if not 30 <= out["pl1_w"] <= 250 or not out["pl1_w"] <= out["pl2_w"] <= 300:
            raise ValueError("need 30 <= pl1_w <= 250 and pl1_w <= pl2_w <= 300")
        out["pl1_window_s"] = int(s["pl1_window_s"])
        if not 1 <= out["pl1_window_s"] <= 56:
            raise ValueError("pl1_window_s must be 1..56")
        if s["fan_mode"] not in FAN_MODES:
            raise ValueError(f"fan_mode must be one of {sorted(FAN_MODES)}")
        out["fan_mode"] = s["fan_mode"]
        for key in ("cpu_curve", "gpu_curve"):
            curve = [int(v) for v in s[key]]
            if len(curve) != 7 or not all(0 <= v <= 100 for v in curve):
                raise ValueError(f"{key} must be 7 values in 0..100")
            out[key] = curve
        return out

    # Hardware
    def apply_hw(self, s):
        """Write settings to hardware; returns a list of error strings."""
        errors = []
        try:
            for cpu in cpu_dirs():
                path = f"{CPU_DIR}/{cpu}/cpufreq/scaling_max_freq"
                if os.path.exists(path):
                    write_text(path, s["max_clock_mhz"] * 1000)
        except OSError as e:
            errors.append(f"clock cap: {e}")
        try:
            write_text(f"{RAPL}/constraint_0_power_limit_uw", s["pl1_w"] * 1_000_000)
            write_text(f"{RAPL}/constraint_0_time_window_us", s["pl1_window_s"] * 1_000_000)
            write_text(f"{RAPL}/constraint_1_power_limit_uw", s["pl2_w"] * 1_000_000)
        except OSError as e:
            errors.append(f"power limits: {e}")
        if not ec_available():
            errors.append("fan control unavailable: msi-ec debug interface missing")
            return errors
        try:
            ec = read_ec()
            ec_guard(ec)
            for base, curve in ((CPU_CURVE, s["cpu_curve"]), (GPU_CURVE, s["gpu_curve"])):
                for i, v in enumerate(curve):
                    if ec[base + i] != v:
                        ec_set(base + i, v)
            if ec[FAN_MODE] != FAN_MODES[s["fan_mode"]]:
                ec_set(FAN_MODE, FAN_MODES[s["fan_mode"]])
        except (OSError, RuntimeError) as e:
            errors.append(f"fan control: {e}")
        return errors

    def drift(self, s):
        """Names of settings whose hardware state differs from s."""
        out = []
        maxes = {read_int(f"{CPU_DIR}/{c}/cpufreq/scaling_max_freq") for c in cpu_dirs()} - {None}
        if maxes != {s["max_clock_mhz"] * 1000}:
            out.append("max_clock_mhz")
        if (read_int(f"{RAPL}/constraint_0_power_limit_uw") or 0) // 1_000_000 != s["pl1_w"]:
            out.append("pl1_w")
        if (read_int(f"{RAPL}/constraint_1_power_limit_uw") or 0) // 1_000_000 != s["pl2_w"]:
            out.append("pl2_w")
        window = (read_int(f"{RAPL}/constraint_0_time_window_us") or 0) / 1e6
        if abs(window - s["pl1_window_s"]) > 0.1 * s["pl1_window_s"]:  # hardware rounds it
            out.append("pl1_window_s")
        if os.path.isdir(MSI_EC):
            try:
                ec = read_ec()
                if list(ec[CPU_CURVE:CPU_CURVE + 7]) != s["cpu_curve"] or \
                        list(ec[GPU_CURVE:GPU_CURVE + 7]) != s["gpu_curve"] or \
                        ec[FAN_MODE] != FAN_MODES[s["fan_mode"]]:
                    out.append("fan")
            except OSError:
                pass
        return out

    def effective(self):
        """Settings that should be on the hardware now (live, unless the failsafe took over the fans)."""
        if self.failsafe:
            return dict(self.live, fan_mode="auto", cpu_curve=FACTORY["cpu_curve"], gpu_curve=FACTORY["gpu_curve"])
        return self.live

    def set_live(self, s):
        with self.lock:
            self.live = self.validate(s)
            self.failsafe = None
            self.hot_samples = 0
            self.errors = self.apply_hw(self.live)
            for e in self.errors:
                log.error(e)
            log.info("applied %s", json.dumps(self.live))

    def enforce_once(self):
        with self.lock:
            if self.live["fan_mode"] == "advanced" and not self.failsafe and os.path.isdir(MSI_EC):
                try:
                    ec = read_ec()
                    hot = ec[CPU_TEMP] >= FAILSAFE_CPU_C or ec[GPU_TEMP] >= FAILSAFE_GPU_C
                    self.hot_samples = self.hot_samples + 1 if hot else 0
                    if self.hot_samples >= FAILSAFE_SAMPLES:
                        self.failsafe = (f"Thermal failsafe: CPU {ec[CPU_TEMP]} C / GPU {ec[GPU_TEMP]} C "
                                         f"with a custom curve; fans returned to the factory curve.")
                        log.warning(self.failsafe)
                except OSError:
                    pass
            target = self.effective()
            drifted = self.drift(target)
            if drifted:
                log.info("re-applying drifted settings: %s", ", ".join(drifted))
                self.errors = self.apply_hw(target)

    def enforce_loop(self):
        while True:
            try:
                self.enforce_once()
            except Exception:  # keep the service alive; the next pass retries
                log.exception("enforcement pass failed")
            time.sleep(ENFORCE_INTERVAL)

    def hw_status(self):
        hw = {}
        cpus = cpu_dirs()
        hw["max_clock_mhz"] = (read_int(f"{CPU_DIR}/cpu0/cpufreq/scaling_max_freq") or 0) // 1000
        cur = [read_int(f"{CPU_DIR}/{c}/cpufreq/scaling_cur_freq") for c in cpus]
        cur = [c for c in cur if c]
        hw["avg_mhz"] = round(sum(cur) / len(cur) / 1000) if cur else None
        hw["cpuinfo_max_mhz"] = self.cpuinfo_max_mhz()
        hw["pl1_w"] = (read_int(f"{RAPL}/constraint_0_power_limit_uw") or 0) // 1_000_000
        hw["pl2_w"] = (read_int(f"{RAPL}/constraint_1_power_limit_uw") or 0) // 1_000_000
        energy, now = read_int(f"{RAPL}/energy_uj"), time.monotonic()
        hw["pkg_w"] = None
        if energy is not None:
            if self.last_energy and now - self.last_energy[1] > 0.2:
                wrap = read_int(f"{RAPL}/max_energy_range_uj") or 2 ** 32
                hw["pkg_w"] = round(((energy - self.last_energy[0]) % wrap) / 1e6 / (now - self.last_energy[1]), 1)
            self.last_energy = (energy, now)
        if os.path.isdir(MSI_EC):
            try:
                ec = read_ec()
                hw.update(
                    cpu_temp=ec[CPU_TEMP], gpu_temp=ec[GPU_TEMP],
                    cpu_rpm=rpm(ec, CPU_TACH), gpu_rpm=rpm(ec, GPU_TACH),
                    cpu_level=ec[CPU_LEVEL], gpu_level=ec[GPU_LEVEL],
                    cpu_thresholds=list(ec[CPU_THRESH:CPU_THRESH + 6]),
                    gpu_thresholds=list(ec[GPU_THRESH:GPU_THRESH + 6]),
                    fan_mode={v: k for k, v in FAN_MODES.items()}.get(ec[FAN_MODE], hex(ec[FAN_MODE])),
                )
            except OSError as e:
                hw["ec_error"] = str(e)
        else:
            hw["ec_error"] = "msi-ec debug interface missing"
        return hw

    # Requests
    def handle(self, req):
        cmd = req.get("cmd")
        with self.lock:
            if cmd == "apply":
                self.set_live(dict(self.live, **req.get("settings", {})))
            elif cmd == "restore":
                which = req.get("which")
                if which not in ("factory", "defaults"):
                    raise ValueError("which must be 'factory' or 'defaults'")
                self.set_live(FACTORY if which == "factory" else self.defaults)
            elif cmd == "save_defaults":
                self.defaults = copy.deepcopy(self.live)
                self._save_defaults()
                log.info("saved defaults %s", json.dumps(self.defaults))
            elif cmd != "status":
                raise ValueError(f"unknown cmd {cmd!r}")
            return {"ok": True, "live": self.live, "defaults": self.defaults, "factory": FACTORY,
                    "hw": self.hw_status(), "failsafe": self.failsafe, "errors": self.errors}


# --- Socket server -------------------------------------------------------------------
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            try:
                resp = self.server.controller.handle(json.loads(line))
            except (ValueError, KeyError, TypeError) as e:
                resp = {"ok": False, "error": str(e)}
            self.wfile.write((json.dumps(resp) + "\n").encode())


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def serve(controller):
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = Server(SOCKET_PATH, Handler)
    server.controller = controller
    gid = grp.getgrnam(SOCKET_GROUP).gr_gid if SOCKET_GROUP else os.getgid()
    os.chown(SOCKET_PATH, 0 if not SYSROOT else os.getuid(), gid)
    os.chmod(SOCKET_PATH, 0o660)
    log.info("listening on %s (group %s)", SOCKET_PATH, SOCKET_GROUP or gid)
    server.serve_forever()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    controller = Controller()
    controller.set_live(controller.defaults)
    threading.Thread(target=controller.enforce_loop, daemon=True).start()
    serve(controller)


if __name__ == "__main__":
    main()
