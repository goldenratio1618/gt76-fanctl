#!/usr/bin/env python3
"""Compare two clock sweeps (e.g. before/after a repaste) from tools/clock_sweep.py.

Left: package temperature at the same moment of each run (--at seconds, default 120) for both
sweeps, so runs of different lengths compare fairly. Right: the AFTER sweep's temperature over time.
Prints a table too. Needs matplotlib.

Usage: compare_sweeps.py BEFORE.json AFTER.json OUT.png [--at 120]
"""
import argparse
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BEFORE_C, AFTER_C, CRITICAL = "#eb6834", "#2a78d6", "#d03b3b"   # validated pair (light surface)
RAMP = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"]
THROTTLE_C = 97


def temp_at(step, t):
    """Package temperature at time t (nearest sample at or before t); None if the run stopped earlier."""
    s = [p for p in step["samples"] if p["t"] <= t + 0.5]
    if not s or step["samples"][-1]["t"] < t - 3:  # runs end a second or two before their nominal length
        return None
    return s[-1]["pkg_c"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("before"); ap.add_argument("after"); ap.add_argument("out")
    ap.add_argument("--at", type=float, default=120)
    a = ap.parse_args()
    before, after = json.load(open(a.before)), json.load(open(a.after))
    bsteps = {s["mhz"]: s for s in before["steps"]}
    asteps = {s["mhz"]: s for s in after["steps"]}
    clocks = sorted(set(bsteps) | set(asteps))

    print(f"{'GHz':>5} {'before@' + str(int(a.at)) + 's':>12} {'after@' + str(int(a.at)) + 's':>11} {'diff':>6} "
          f"{'after final':>12} {'after s':>8} {'after W':>8} {'after GFLOP/s':>14}  note")
    rows = []
    for mhz in clocks:
        b, f = bsteps.get(mhz), asteps.get(mhz)
        tb = temp_at(b, a.at) if b else None
        ta = temp_at(f, a.at) if f else None
        rows.append((mhz, tb, ta))
        note = "; ".join(x for x in (b and b["too_hot"] and "before: throttled", f and f["too_hot"] and f"after: {f['stop_reason']}") if x)
        fmt = lambda v: f"{v:.0f} C" if v is not None else "-"
        print(f"{mhz / 1000:5.1f} {fmt(tb):>12} {fmt(ta):>11} {(f'{ta - tb:+.0f}' if tb is not None and ta is not None else '-'):>6} "
              f"{(fmt(f['final_pkg_c']) if f else '-'):>12} {(f['seconds'] if f else '-'):>8} "
              f"{(f['mean_pkg_w_last_30s'] if f else '-'):>8} {(f['mean_gflops'] if f else '-'):>14}  {note}")

    plt.rcParams.update({"font.size": 10, "text.color": INK, "axes.labelcolor": INK2, "xtick.color": INK2,
                         "ytick.color": INK2, "axes.edgecolor": GRID, "axes.facecolor": SURFACE,
                         "figure.facecolor": SURFACE})
    fig, (l, r) = plt.subplots(1, 2, figsize=(12.5, 5.2), gridspec_kw={"width_ratios": [1, 1.25]})
    for ax in (l, r):
        ax.grid(True, color=GRID, linewidth=0.8); ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.axhline(THROTTLE_C, color=CRITICAL, linewidth=1.5, linestyle=(0, (4, 3)))

    ghz = [m / 1000 for m in clocks]
    for label, color, idx, dy in (("Before", BEFORE_C, 1, 10), ("After", AFTER_C, 2, -16)):
        xs = [g for g, row in zip(ghz, rows) if row[idx] is not None]
        ys = [row[idx] for row in rows if row[idx] is not None]
        l.plot(xs, ys, color=color, linewidth=2, marker="o", markersize=8, markeredgecolor=SURFACE,
               markeredgewidth=2, label=label)
        for x, y in zip(xs, ys):
            l.annotate(f"{y:.0f}", (x, y), textcoords="offset points", xytext=(0, dy), ha="center", color=INK, fontsize=9)
    l.text(ghz[0], THROTTLE_C + 0.4, f"CPU throttles at {THROTTLE_C} °C", color=INK2, fontsize=9, va="bottom")
    l.set_xticks(ghz, [f"{g:.1f}" for g in ghz])
    l.set_xlabel("Max clock (GHz)"); l.set_ylabel("CPU package temperature (°C)")
    l.set_title(f"Temperature after {a.at:.0f} s of full load", loc="left", color=INK, fontsize=11, fontweight="bold")
    l.legend(frameon=False, loc="lower right", labelcolor=INK)
    allv = [v for row in rows for v in row[1:] if v is not None]
    l.set_ylim(min(allv) - 6, THROTTLE_C + 3)

    for i, s in enumerate(after["steps"]):
        t = [p["t"] for p in s["samples"]]; y = [p["pkg_c"] for p in s["samples"]]
        r.plot(t, y, color=RAMP[i % len(RAMP)], linewidth=2, label=f"{s['mhz'] / 1000:.1f} GHz")
        r.annotate(f"{s['mhz'] / 1000:.1f}", (t[-1], y[-1]), textcoords="offset points", xytext=(5, 0),
                   va="center", color=INK, fontsize=9)
    r.axvline(a.at, color=INK2, linewidth=1, linestyle=":")
    r.text(a.at, THROTTLE_C - 0.6, f" {a.at:.0f} s", color=INK2, fontsize=8.5, va="top")
    r.set_xlabel("Seconds of full load"); r.set_ylabel("CPU package temperature (°C)")
    r.set_title("After: package temperature during each run", loc="left", color=INK, fontsize=11, fontweight="bold")
    r.legend(frameon=False, loc="lower center", ncol=len(after["steps"]), labelcolor=INK, fontsize=9,
             columnspacing=1.2, handlelength=1.5)
    fan = after["fan_settings"]
    fig.suptitle(f"All-core AVX2 load, power limits off, CPU fan curve {'/'.join(map(str, fan['cpu_curve']))} % "
                 f"({fan['fan_mode']} mode)", color=INK2, fontsize=9.5, x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(a.out, dpi=140)
    print("saved", a.out)


if __name__ == "__main__":
    main()
