#!/usr/bin/env python3
"""Clock-cap vs temperature sweep through gt76-fanctld (no root needed).

For each clock cap: wait until the CPU cools to --cool-to, apply the cap with power limits
effectively off, run an all-core AVX load for --seconds while sampling every second, then
summarize. A step that throttles (hottest core at the TCC limit, kernel throttle counters
rising) or trips the service's thermal failsafe is marked too hot, and higher clocks are
skipped. The fan settings in effect when the sweep starts are kept for every step, and all
original settings are restored at the end.

Usage: clock_sweep.py OUT.json [MHz ...]      (default 3800 3900 4000 4100 4200)
"""
import argparse
import glob
import json
import os
import signal
import socket
import subprocess
import sys
import time

SOCKET_PATH = os.environ.get("FANCTL_SOCKET", "/run/gt76-fanctl.sock")
STRESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cpu_stress.py")
THROTTLE_CORE_C = 97  # i9-10900K TjMax 100 C with this BIOS's TCC offset of 3


def request(req):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(10)
        s.connect(SOCKET_PATH)
        s.sendall((json.dumps(req) + "\n").encode())
        resp = json.loads(s.makefile().readline())
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error"))
    return resp


def coretemp_dir():
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        if open(f"{d}/name").read().strip() == "coretemp":
            return d
    raise RuntimeError("coretemp hwmon not found")


def core_temps(d):
    """(package temp, hottest core temp) in C from coretemp."""
    temps = {}
    for f in glob.glob(f"{d}/temp*_input"):
        label = open(f.replace("_input", "_label")).read().strip()
        temps[label] = int(open(f).read()) / 1000
    pkg = temps.pop("Package id 0", None)
    return pkg, max(temps.values())


def throttle_counts():
    total = 0
    for f in glob.glob("/sys/devices/system/cpu/cpu*/thermal_throttle/*_throttle_count"):
        total += int(open(f).read())
    return total


def sample(ct, t0, base_throttle):
    st = request({"cmd": "status"})
    hw = st["hw"]
    pkg, hottest = core_temps(ct)
    return {
        "t": round(time.time() - t0, 1), "ec_cpu_c": hw.get("cpu_temp"), "pkg_c": pkg, "hottest_core_c": hottest,
        "cpu_rpm": hw.get("cpu_rpm"), "cpu_level": hw.get("cpu_level"), "gpu_c": hw.get("gpu_temp"),
        "gpu_rpm": hw.get("gpu_rpm"), "pkg_w": hw.get("pkg_w"), "avg_mhz": hw.get("avg_mhz"),
        "throttle_events": throttle_counts() - base_throttle, "failsafe": st.get("failsafe"),
    }


def summarize(mhz, samples, gflops, stop_reason):
    tail = [s for s in samples if s["t"] >= samples[-1]["t"] - 30]
    mean = lambda k: round(sum(s[k] for s in tail if s[k] is not None) / max(1, sum(s[k] is not None for s in tail)), 1)
    slope = (tail[-1]["pkg_c"] - tail[0]["pkg_c"]) / max(1e-6, tail[-1]["t"] - tail[0]["t"]) * 60
    return {
        "mhz": mhz, "seconds": samples[-1]["t"], "too_hot": bool(stop_reason), "stop_reason": stop_reason,
        "final_pkg_c": samples[-1]["pkg_c"], "final_ec_cpu_c": samples[-1]["ec_cpu_c"],
        "max_pkg_c": max(s["pkg_c"] for s in samples), "max_hottest_core_c": max(s["hottest_core_c"] for s in samples),
        "slope_last_30s_c_per_min": round(slope, 1), "mean_pkg_w_last_30s": mean("pkg_w"),
        "mean_cpu_rpm_last_30s": mean("cpu_rpm"), "mean_avg_mhz_last_30s": mean("avg_mhz"),
        "mean_gflops": round(sum(gflops) / len(gflops), 1) if gflops else None,
        "samples": samples,
    }


def run_step(mhz, seconds, cool_to, ct, original):
    print(f"\n=== {mhz / 1000:.1f} GHz: cooling to {cool_to} C ...", flush=True)
    request({"cmd": "apply", "settings": dict(original, max_clock_mhz=mhz, pl1_w=250, pl2_w=300)})
    deadline = time.time() + 300
    while request({"cmd": "status"})["hw"]["cpu_temp"] > cool_to and time.time() < deadline:
        time.sleep(5)
    print(f"    load for {seconds} s", flush=True)
    base_throttle = throttle_counts()
    stress = subprocess.Popen([sys.executable, STRESS, "--minutes", str(seconds / 60), "--interval", "10"],
                              stdout=subprocess.PIPE, text=True, start_new_session=True)
    t0, samples, stop_reason = time.time(), [], None
    while stress.poll() is None:
        time.sleep(1)
        s = sample(ct, t0, base_throttle)
        samples.append(s)
        if s["t"] % 10 < 1:
            print(f"    t={s['t']:5.0f}s pkg {s['pkg_c']:.0f} C (hottest core {s['hottest_core_c']:.0f}) "
                  f"fan {s['cpu_rpm']} rpm {s['pkg_w']} W {s['avg_mhz']} MHz", flush=True)
        if s["failsafe"]:
            stop_reason = "service thermal failsafe tripped (CPU >= 95 C)"
        elif s["hottest_core_c"] >= THROTTLE_CORE_C or s["throttle_events"] > 0:
            stop_reason = f"thermal throttling (hottest core {s['hottest_core_c']:.0f} C, {s['throttle_events']} throttle events)"
        if stop_reason:
            stress.terminate()
            break
    try:
        out = stress.communicate(timeout=15)[0]
    except subprocess.TimeoutExpired:  # backstop: kill the whole load, workers included
        os.killpg(stress.pid, signal.SIGKILL)
        out = stress.communicate()[0]
    if not stop_reason:  # the last sample can land after the load ended; keep only in-load samples
        samples = [s for s in samples if s["t"] <= seconds] or samples
    gflops = [float(line.split()[5]) for line in out.splitlines()
              if line.strip() and line.strip()[0].isdigit() and len(line.split()) >= 6]
    result = summarize(mhz, samples, gflops, stop_reason)
    print(f"    -> final {result['final_pkg_c']} C, slope {result['slope_last_30s_c_per_min']} C/min, "
          f"{result['mean_pkg_w_last_30s']} W, {result['mean_gflops']} GFLOP/s" + (f"  TOO HOT: {stop_reason}" if stop_reason else ""),
          flush=True)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("mhz", nargs="*", type=int, default=[3800, 3900, 4000, 4100, 4200])
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--cool-to", type=int, default=50)
    args = ap.parse_args()
    ct = coretemp_dir()
    original = request({"cmd": "status"})["live"]
    print("Fan settings for every step:", original["fan_mode"], original["cpu_curve"], flush=True)
    results = {"fan_settings": {k: original[k] for k in ("fan_mode", "cpu_curve", "gpu_curve")}, "steps": []}
    try:
        for mhz in args.mhz:
            step = run_step(mhz, args.seconds, args.cool_to, ct, original)
            results["steps"].append(step)
            with open(args.out, "w") as f:
                json.dump(results, f)
            if step["too_hot"]:
                print("Stopping: higher clocks would be hotter still.", flush=True)
                break
    finally:
        request({"cmd": "apply", "settings": original})
        print("Restored original settings:", original, flush=True)


if __name__ == "__main__":
    main()
