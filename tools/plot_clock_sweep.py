#!/usr/bin/env python3
"""Plot clock_sweep.json: temperature after the load vs clock cap, and temperature over time per clock.
Usage: python3 tools/plot_clock_sweep.py SWEEP.json OUT.png   (needs matplotlib)"""
import json
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
S1, S2, CRITICAL = "#2a78d6", "#eb6834", "#d03b3b"
RAMP = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"]  # ordinal, validated
THROTTLE_C = 97

data = json.load(open(sys.argv[1]))
steps = data["steps"]
fan = data["fan_settings"]

plt.rcParams.update({"font.size": 10, "text.color": INK, "axes.labelcolor": INK2, "xtick.color": INK2,
                     "ytick.color": INK2, "axes.edgecolor": GRID, "axes.facecolor": SURFACE,
                     "figure.facecolor": SURFACE})
fig, (a, b) = plt.subplots(1, 2, figsize=(12.5, 5.2), gridspec_kw={"width_ratios": [1, 1.25]})

def style(ax):
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

# Left: end-of-run temperature vs clock
ghz = [s["mhz"] / 1000 for s in steps]
final = [s["final_pkg_c"] for s in steps]
peak = [s["max_hottest_core_c"] for s in steps]
style(a)
a.axhline(THROTTLE_C, color=CRITICAL, linewidth=1.5, linestyle=(0, (4, 3)))
a.text(ghz[0], THROTTLE_C + 0.4, f"CPU throttles at {THROTTLE_C} °C", color=INK2, fontsize=9, va="bottom")
a.plot(ghz, final, color=S1, linewidth=2, marker="o", markersize=8, markeredgecolor=SURFACE, markeredgewidth=2)
for s, x, y in zip(steps, ghz, final):
    a.annotate(f"{y:.0f}", (x, y), textcoords="offset points", xytext=(0, -16), ha="center", color=INK, fontsize=9)
    if s["too_hot"]:
        a.annotate(f"throttling,\nstopped at {s['seconds']:.0f} s", (x, y), textcoords="offset points", xytext=(6, -30),
                   ha="right", va="top", color=INK, fontsize=9, fontweight="bold")
a.set_xticks(ghz, [f"{g:.1f}" for g in ghz])
a.set_xlabel("Max clock (GHz)")
a.set_ylabel("CPU package temperature (°C)")
lo = min(final) - 6
a.set_ylim(lo, max(THROTTLE_C + 3, max(peak) + 3))
a.set_title("Temperature after 2 min of full load", loc="left", color=INK, fontsize=11, fontweight="bold")
a.text(0.99, 0.02, "Hottest core was within 1 °C of the package at every step", transform=a.transAxes,
       ha="right", va="bottom", color=INK2, fontsize=8.5)

# Right: package temperature over time, one line per clock
style(b)
b.axhline(THROTTLE_C, color=CRITICAL, linewidth=1.5, linestyle=(0, (4, 3)))
for i, s in enumerate(steps):
    t = [p["t"] for p in s["samples"]]
    y = [p["pkg_c"] for p in s["samples"]]
    color = RAMP[i % len(RAMP)]
    b.plot(t, y, color=color, linewidth=2, label=f"{s['mhz'] / 1000:.1f} GHz")
    b.annotate(f"{s['mhz'] / 1000:.1f}", (t[-1], y[-1]), textcoords="offset points", xytext=(5, 0),
               va="center", color=INK, fontsize=9)
b.set_xlabel("Seconds of full load")
b.set_ylabel("CPU package temperature (°C)")
b.set_ylim(lo - 15, max(THROTTLE_C + 3, max(peak) + 3))
b.set_title("Package temperature during each run", loc="left", color=INK, fontsize=11, fontweight="bold")
b.legend(frameon=False, loc="lower right", labelcolor=INK, title="Clock cap", title_fontsize=9)

fig.suptitle(f"All-core AVX2 load, CPU fan curve {'/'.join(map(str, fan['cpu_curve']))} % ({fan['fan_mode']} mode), "
             f"power limits off", color=INK2, fontsize=9.5, x=0.01, ha="left")
fig.tight_layout(rect=(0, 0, 1, 0.96))
fig.savefig(sys.argv[2], dpi=140)
print("saved", sys.argv[2])
