#!/usr/bin/env python3
"""GPU heat-soak benchmark: sustained matrix multiplies on the NVIDIA GPU, sampled every second.

Waits until the GPU cools to --cool-to, runs back-to-back matrix multiplies for --seconds, and
records GPU temperature, graphics clock, power, slowdown reasons, fan speed (via gt76-fanctld,
no root needed) and throughput. Fan settings are left as they are, so runs are comparable only
under the same fan curve. Stops early if the service's thermal failsafe trips or the GPU reaches
--max-temp. Ctrl+C stops the load and still writes what was recorded.

Needs a Python with CUDA PyTorch, e.g.:
  ~/src/photo-sorter/.venv/bin/python tools/gpu_stress.py OUT.json
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time

SOCKET_PATH = os.environ.get("FANCTL_SOCKET", "/run/gt76-fanctl.sock")
SMI_FIELDS = ["temperature.gpu", "clocks.gr", "power.draw", "utilization.gpu",
              "clocks_event_reasons.sw_power_cap", "clocks_event_reasons.sw_thermal_slowdown",
              "clocks_event_reasons.hw_thermal_slowdown", "clocks_event_reasons.hw_slowdown"]
REASONS = ["power_cap", "thermal_slowdown", "hw_thermal_slowdown", "hw_slowdown"]


def fanctl_status():
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(5)
            s.connect(SOCKET_PATH)
            s.sendall(b'{"cmd": "status"}\n')
            resp = json.loads(s.makefile().readline())
        return resp if resp.get("ok") else None
    except OSError:
        return None


def smi():
    out = subprocess.run(["nvidia-smi", "--query-gpu=" + ",".join(SMI_FIELDS), "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, timeout=10, check=True).stdout.strip().split("\n")[0]
    v = [x.strip() for x in out.split(",")]
    num = lambda x: float(x) if x.replace(".", "", 1).isdigit() else None
    return {"gpu_c": num(v[0]), "gpu_mhz": num(v[1]), "gpu_w": num(v[2]), "util_pct": num(v[3]),
            "slowdown": [r for r, x in zip(REASONS, v[4:]) if x == "Active"]}


def wait_cool(target, timeout):
    t0 = time.time()
    while (c := smi()["gpu_c"]) is not None and c > target and time.time() - t0 < timeout:
        print(f"\rcooling: GPU {c:.0f} C > {target} C", end="", flush=True)
        time.sleep(5)
    print()


class Load(threading.Thread):
    """Back-to-back matmuls; self.flops counts completed work."""

    def __init__(self, torch, size, dtype):
        super().__init__(daemon=True)
        self.torch, self.size = torch, size
        dev = torch.device("cuda")
        self.a = torch.randn(size, size, device=dev, dtype=dtype)
        self.b = torch.randn(size, size, device=dev, dtype=dtype)
        self.flops, self.stop = 0.0, threading.Event()

    def run(self):
        per = 2 * self.size ** 3
        while not self.stop.is_set():
            for _ in range(4):
                self.torch.mm(self.a, self.b)
            self.torch.cuda.synchronize()
            self.flops += 4 * per


def summarize(samples, stop_reason, args, gpu_name):
    tail = [s for s in samples if s["t"] >= samples[-1]["t"] - 60]
    mean = lambda k: round(sum(s[k] for s in tail if s[k] is not None) / max(1, sum(s[k] is not None for s in tail)), 1)
    slowed = lambda r: round(sum(r in s["slowdown"] for s in tail) / len(tail), 2)
    return {
        "gpu": gpu_name, "dtype": args.dtype, "size": args.size, "seconds": samples[-1]["t"], "stop_reason": stop_reason,
        "last60s": {k: mean(k) for k in ("gpu_c", "gpu_mhz", "gpu_w", "tflops", "gpu_rpm", "cpu_c")}
                   | {f"frac_{r}": slowed(r) for r in REASONS},
        "peak_gpu_c": max(s["gpu_c"] for s in samples if s["gpu_c"] is not None),
        "gpu_c_slope_per_min_last60s": round((tail[-1]["gpu_c"] - tail[0]["gpu_c"]) / max(1e-6, tail[-1]["t"] - tail[0]["t"]) * 60, 2),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", nargs="?", help="write samples and summary as JSON here")
    ap.add_argument("--seconds", type=float, default=300)
    ap.add_argument("--cool-to", type=float, default=50, help="wait until the GPU is at or below this before starting (C)")
    ap.add_argument("--max-temp", type=float, default=95, help="stop early at this GPU temperature (C)")
    ap.add_argument("--dtype", choices=["fp16", "fp32"], default="fp16")
    ap.add_argument("--size", type=int, default=8192, help="matrix size N (N x N)")
    args = ap.parse_args()

    try:
        import torch
    except ImportError:
        sys.exit("PyTorch isn't installed for this Python; run with a Python that has CUDA PyTorch (see --help).")
    if not torch.cuda.is_available():
        sys.exit("PyTorch can't see a CUDA GPU.")
    gpu_name = torch.cuda.get_device_name(0)
    fan = fanctl_status()
    fan_settings = fan and {k: fan["live"][k] for k in ("fan_mode", "cpu_curve", "gpu_curve", "max_clock_mhz")}
    if fan is None:
        print("note: gt76-fanctld not reachable; fan speeds won't be recorded")

    wait_cool(args.cool_to, timeout=900)
    load = Load(torch, args.size, torch.float16 if args.dtype == "fp16" else torch.float32)
    samples, stop_reason = [], None
    t0 = time.time()
    load.start()
    last_t, last_flops = t0, 0.0
    print(f"{gpu_name}, {args.dtype} {args.size}x{args.size} matmul, {args.seconds:.0f} s")
    print("    t   GPU C   MHz      W  TFLOP/s  fan rpm  CPU C  slowdown")
    try:
        while time.time() - t0 < args.seconds:
            time.sleep(max(0.0, 1 - (time.time() - last_t)))
            now, flops = time.time(), load.flops
            s = {"t": round(now - t0, 1), **smi(), "tflops": round((flops - last_flops) / (now - last_t) / 1e12, 2)}
            last_t, last_flops = now, flops
            st = fanctl_status()
            hw = st["hw"] if st else {}
            s.update(gpu_rpm=hw.get("gpu_rpm"), gpu_level=hw.get("gpu_level"), cpu_c=hw.get("cpu_temp"),
                     failsafe=st.get("failsafe") if st else None)
            samples.append(s)
            if len(samples) % 5 == 0:
                print(f"{s['t']:5.0f}  {s['gpu_c']:6.0f}  {s['gpu_mhz']:4.0f}  {s['gpu_w']:5.0f}  {s['tflops']:7.1f}  "
                      f"{s['gpu_rpm'] or 0:7d}  {s['cpu_c'] or 0:5d}  {','.join(s['slowdown']) or '-'}", flush=True)
            if s["failsafe"]:
                stop_reason = f"service failsafe: {s['failsafe']}"
                break
            if s["gpu_c"] is not None and s["gpu_c"] >= args.max_temp:
                stop_reason = f"GPU reached {s['gpu_c']:.0f} C"
                break
    except KeyboardInterrupt:
        stop_reason = "interrupted"
    finally:
        load.stop.set()
        load.join(timeout=30)

    if not samples:
        return
    summary = summarize(samples, stop_reason, args, gpu_name)
    print(json.dumps(summary, indent=2))
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"summary": summary, "fan_settings": fan_settings,
                       "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0)), "samples": samples}, f, indent=1)
        print("wrote", args.out)


if __name__ == "__main__":
    main()
