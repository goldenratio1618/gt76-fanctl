#!/usr/bin/env python3
"""All-core CPU stress test with a live throughput readout.

Runs one worker per logical CPU. Every few seconds prints aggregate throughput,
average core clock, package temperature and the current RAPL power limits, so
you can change the limits in another terminal and see the effect.

  --mode avx     numpy matrix multiplies (AVX2/FMA, the hottest realistic load)
  --mode scalar  pure-Python integer arithmetic (lighter, like branchy code)

Stops after --minutes (default 60) or on Ctrl-C.
"""
import argparse
import glob
import multiprocessing as mp
import os
import signal
import time

# One BLAS thread per worker so each process occupies exactly one logical CPU.
for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[var] = "1"

RAPL = "/sys/class/powercap/intel-rapl:0"


def run_worker(target, cpu, counter, idx):
    """Run a worker only on logical CPU `cpu` (None = unpinned)."""
    signal.signal(signal.SIGTERM, signal.SIG_DFL)  # the parent's handler is inherited; workers just exit
    if cpu is not None:
        os.sched_setaffinity(0, {cpu})
    target(counter, idx)


def worker_avx(counter, idx):
    import numpy as np
    n = 128
    a = np.random.rand(n, n)
    b = np.random.rand(n, n)
    while True:
        for _ in range(50):
            a @ b
        counter[idx] += 50 * 2 * n**3 / 1e9  # GFLOP done


def worker_scalar(counter, idx):
    while True:
        x = 0
        for i in range(200_000):
            x = (x * 31 + i) & 0xFFFFFFFF
        counter[idx] += 0.2  # million loop iterations done


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read())
    except OSError:
        return None


def pkg_temp():
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            with open(f"{hw}/name") as f:
                if f.read().strip() == "coretemp":
                    return read_int(f"{hw}/temp1_input") / 1000
        except (OSError, TypeError):
            pass
    return None


def avg_mhz():
    freqs = [read_int(p) for p in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")]
    freqs = [f for f in freqs if f]
    return sum(freqs) / len(freqs) / 1000 if freqs else None


def energy_uj():
    """Package energy counter in microjoules (root only); None if unreadable."""
    return read_int(f"{RAPL}/energy_uj")


def limits():
    pl1 = read_int(f"{RAPL}/constraint_0_power_limit_uw")
    pl2 = read_int(f"{RAPL}/constraint_1_power_limit_uw")
    fmt = lambda v: f"{v / 1e6:.0f}W" if v is not None else "?"
    return fmt(pl1), fmt(pl2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["avx", "scalar"], default="avx")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--interval", type=float, default=5)
    ap.add_argument("--cpus", help="comma-separated logical CPUs to pin workers to, one each (e.g. 0,1,2,3,4)")
    args = ap.parse_args()
    cpus = [int(c) for c in args.cpus.split(",")] if args.cpus else None
    if cpus:
        args.workers = len(cpus)

    # Treat SIGTERM like Ctrl-C so the finally block stops the workers instead of orphaning them
    def on_term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)
    unit = "GFLOP/s" if args.mode == "avx" else "M iter/s"
    target = worker_avx if args.mode == "avx" else worker_scalar
    counter = mp.Array("d", args.workers, lock=False)
    procs = [mp.Process(target=run_worker, args=(target, cpus[i] if cpus else None, counter, i), daemon=True)
             for i in range(args.workers)]
    for p in procs:
        p.start()

    print(f"Stressing {args.workers} workers, mode={args.mode}, auto-stop after {args.minutes:g} min. Ctrl-C to stop.")
    last_e = energy_uj()
    wrap = read_int(f"{RAPL}/max_energy_range_uj") or 0
    print(f"{'time':>6}  {'PL1':>5} {'PL2':>5}  {'avg MHz':>7}  {'temp':>5}  {unit:>10}" + ("  pkg W" if last_e is not None else ""))
    start = last_t = time.time()
    last_total = 0.0
    try:
        while time.time() - start < args.minutes * 60:
            time.sleep(args.interval)
            now, total = time.time(), sum(counter)
            rate = (total - last_total) / (now - last_t)
            watts = ""
            if last_e is not None:
                e = energy_uj()
                watts = f"  {((e - last_e) % wrap if wrap else e - last_e) / 1e6 / (now - last_t):5.0f}"
                last_e = e
            last_t, last_total = now, total
            pl1, pl2 = limits()
            mhz, temp = avg_mhz(), pkg_temp()
            el = int(now - start)
            print(f"{el // 60:3d}:{el % 60:02d}  {pl1:>5} {pl2:>5}  {mhz or 0:7.0f}  {temp or 0:4.0f}C  {rate:10.1f}{watts}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
        print("Stopped.")


if __name__ == "__main__":
    main()
