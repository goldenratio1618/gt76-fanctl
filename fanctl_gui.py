#!/usr/bin/env python3
"""GT76 Fan Control: desktop UI for gt76-fanctld (fan curves, CPU clock cap, temperatures).

Runs as the normal user; all hardware changes go through the root service's socket.
"""
import html
import json
import os
import socket
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QRadioButton, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

SOCKET_PATH = os.environ.get("FANCTL_SOCKET", "/run/gt76-fanctl.sock")
CLOCK_MIN_MHZ = 1000
BANDS = 7


def request(req, timeout=5):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(SOCKET_PATH)
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    resp = json.loads(buf)
    if not resp.get("ok"):
        raise ValueError(resp.get("error", "request failed"))
    return resp


def pl1_for_clock(mhz):
    """Sustained power limit to pair with a clock cap: the measured full-AVX draw at that
    clock (about 110 W at 3.8 GHz, scaling with the cube of clock) plus 15 W, so it only
    binds for unusually heavy loads. Rounded to 5 W."""
    watts = 110 * (mhz / 3800) ** 3 + 15
    return int(min(250, max(60, 5 * round(watts / 5))))


def band_labels(thresholds):
    t = thresholds
    return [f"<{t[0]}"] + [f"{a}–{b}" for a, b in zip(t, t[1:])] + [f"{t[-1]}+"]


def active_band(temp, thresholds):
    return sum(temp >= t for t in thresholds)


class CurveRow:
    """Seven slider+spinbox columns for one fan."""

    def __init__(self, grid, row, name):
        self.title = QLabel(f"<b>{name}</b>")
        grid.addWidget(self.title, row, 0, 3, 1)
        self.band_labels, self.sliders, self.spins = [], [], []
        for i in range(BANDS):
            lab = QLabel("")
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sl = QSlider(Qt.Orientation.Vertical)
            sl.setRange(0, 100)
            sl.setMinimumHeight(110)
            sp = QSpinBox()
            sp.setRange(0, 100)
            sp.setSuffix("%")
            sl.valueChanged.connect(sp.setValue)
            sp.valueChanged.connect(sl.setValue)
            grid.addWidget(lab, row, i + 1)
            grid.addWidget(sl, row + 1, i + 1, alignment=Qt.AlignmentFlag.AlignHCenter)
            grid.addWidget(sp, row + 2, i + 1)
            self.band_labels.append(lab)
            self.sliders.append(sl)
            self.spins.append(sp)
        self.all_spin = QSpinBox()
        self.all_spin.setRange(0, 100)
        self.all_spin.setSuffix("%")
        self.all_button = QPushButton("Set all bands")
        self.all_button.clicked.connect(lambda: self.set_values([self.all_spin.value()] * BANDS))
        box = QVBoxLayout()
        box.addStretch()
        box.addWidget(self.all_spin)
        box.addWidget(self.all_button)
        box.addStretch()
        grid.addLayout(box, row, BANDS + 1, 3, 1)

    def values(self):
        return [sp.value() for sp in self.spins]

    def set_values(self, values):
        for sp, v in zip(self.spins, values):
            sp.setValue(v)

    def set_bands(self, thresholds, active):
        for i, (lab, text) in enumerate(zip(self.band_labels, band_labels(thresholds))):
            text = html.escape(f"{text} °C")  # "<50" would otherwise parse as a tag
            lab.setText(f"<b>▶ {text}</b>" if i == active else text)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GT76 Fan Control")
        self.status = None
        self.loaded = False
        root = QWidget()
        layout = QVBoxLayout(root)
        self.setCentralWidget(root)

        # Live readings
        now = QGroupBox("Now")
        g = QGridLayout(now)
        self.readouts = {}
        big = QFont()
        big.setPointSize(big.pointSize() + 3)
        for col, (key, title) in enumerate([("cpu_temp", "CPU temp"), ("gpu_temp", "GPU temp"),
                                             ("cpu_fan", "CPU fan"), ("gpu_fan", "GPU fan"),
                                             ("clock", "CPU clock"), ("power", "CPU package")]):
            t = QLabel(title)
            v = QLabel("–")
            v.setFont(big)
            g.addWidget(t, 0, col)
            g.addWidget(v, 1, col)
            self.readouts[key] = v
        layout.addWidget(now)

        # Clock cap
        clock = QGroupBox("Max CPU clock (all cores)")
        h = QHBoxLayout(clock)
        self.clock_slider = QSlider(Qt.Orientation.Horizontal)
        self.clock_slider.setRange(CLOCK_MIN_MHZ // 100, 53)
        self.clock_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.clock_slider.setTickInterval(5)
        self.clock_label = QLabel()
        self.clock_label.setMinimumWidth(260)
        self.clock_slider.valueChanged.connect(self.update_clock_label)
        self.clock_apply = QPushButton("Apply clock")
        self.clock_apply.clicked.connect(self.apply_clock)
        h.addWidget(self.clock_slider, 1)
        h.addWidget(self.clock_label)
        h.addWidget(self.clock_apply)
        layout.addWidget(clock)

        # Fan curves
        fans = QGroupBox("Fan curves (fan speed % per temperature band)")
        v = QVBoxLayout(fans)
        modes = QHBoxLayout()
        self.mode_auto = QRadioButton("Auto (EC factory behaviour; curves below are ignored)")
        self.mode_adv = QRadioButton("Advanced (EC follows the curves below)")
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.mode_auto)
        self.mode_group.addButton(self.mode_adv)
        modes.addWidget(self.mode_auto)
        modes.addWidget(self.mode_adv)
        modes.addStretch()
        v.addLayout(modes)
        grid = QGridLayout()
        self.cpu_row = CurveRow(grid, 0, "CPU fan")
        self.gpu_row = CurveRow(grid, 3, "GPU fan")
        v.addLayout(grid)
        note = QLabel("Fans ramp gradually: about 45 s (CPU) to 90 s (GPU) to reach a new speed. "
                      "If a custom curve lets the CPU reach 95 °C or the GPU 90 °C, the service "
                      "switches the fans back to the factory curve.")
        note.setWordWrap(True)
        v.addWidget(note)
        self.fan_apply = QPushButton("Apply fan settings")
        self.fan_apply.clicked.connect(self.apply_fans)
        v.addWidget(self.fan_apply, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addWidget(fans)

        # Restore / save
        buttons = QHBoxLayout()
        for text, slot in [("Restore factory", lambda: self.restore("factory")),
                           ("Restore my defaults", lambda: self.restore("defaults")),
                           ("Save current as my defaults", self.save_defaults)]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        layout.addLayout(buttons)
        self.defaults_label = QLabel()
        self.defaults_label.setWordWrap(True)
        layout.addWidget(self.defaults_label)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    # Helpers
    def update_clock_label(self):
        mhz = self.clock_slider.value() * 100
        self.clock_label.setText(f"{mhz / 1000:.1f} GHz  (sustained power limit {pl1_for_clock(mhz)} W)")

    def show_error(self, text):
        self.message.setText(f"<span style='color:#c0392b'>{text}</span>")

    def load_controls(self, live):
        self.clock_slider.setValue(live["max_clock_mhz"] // 100)
        self.update_clock_label()
        (self.mode_adv if live["fan_mode"] == "advanced" else self.mode_auto).setChecked(True)
        self.cpu_row.set_values(live["cpu_curve"])
        self.gpu_row.set_values(live["gpu_curve"])

    def describe(self, s):
        return (f"{s['max_clock_mhz'] / 1000:.1f} GHz cap, PL1 {s['pl1_w']} W, fans {s['fan_mode']} "
                f"(CPU {'/'.join(map(str, s['cpu_curve']))}, GPU {'/'.join(map(str, s['gpu_curve']))})")

    def send(self, req, done=None):
        try:
            self.status = request(req)
        except (OSError, ValueError) as e:
            self.show_error(f"Request failed: {e}")
            return False
        self.load_controls(self.status["live"])
        self.message.setText(done or "")
        self.show_status()
        return True

    # Actions
    def apply_clock(self):
        mhz = self.clock_slider.value() * 100
        self.send({"cmd": "apply", "settings": {"max_clock_mhz": mhz, "pl1_w": pl1_for_clock(mhz)}},
                  f"Clock cap set to {mhz / 1000:.1f} GHz.")

    def apply_fans(self):
        mode = "advanced" if self.mode_adv.isChecked() else "auto"
        self.send({"cmd": "apply", "settings": {"fan_mode": mode, "cpu_curve": self.cpu_row.values(),
                                                "gpu_curve": self.gpu_row.values()}},
                  f"Fan settings applied ({mode} mode).")

    def restore(self, which):
        self.send({"cmd": "restore", "which": which},
                  "Restored factory settings." if which == "factory" else "Restored your defaults.")

    def save_defaults(self):
        if not self.status:
            return
        live = self.status["live"]
        if QMessageBox.question(self, "Save defaults",
                                f"Apply these at every boot?\n\n{self.describe(live)}") \
                == QMessageBox.StandardButton.Yes:
            self.send({"cmd": "save_defaults"}, "Saved as your defaults.")

    # Refresh
    def refresh(self):
        try:
            self.status = request({"cmd": "status"})
        except (OSError, ValueError) as e:
            self.show_error(f"Can't reach the gt76-fanctld service ({e}). "
                            "Check: systemctl status gt76-fanctld")
            return
        if not self.loaded:
            self.load_controls(self.status["live"])
            self.loaded = True
        if self.message.text().startswith("<span"):  # clear a stale error; show_status re-raises live ones
            self.message.clear()
        self.show_status()

    def show_status(self):
        st, hw = self.status, self.status["hw"]
        r = self.readouts
        if "ec_error" in hw:
            for k in ("cpu_temp", "gpu_temp", "cpu_fan", "gpu_fan"):
                r[k].setText("n/a")
            self.show_error(f"EC unavailable: {hw['ec_error']}")
        else:
            r["cpu_temp"].setText(f"{hw['cpu_temp']} °C")
            r["gpu_temp"].setText(f"{hw['gpu_temp']} °C")
            r["cpu_fan"].setText(f"{hw['cpu_rpm']} rpm ({hw['cpu_level']}%)")
            r["gpu_fan"].setText(f"{hw['gpu_rpm']} rpm ({hw['gpu_level']}%)")
            self.cpu_row.set_bands(hw["cpu_thresholds"], active_band(hw["cpu_temp"], hw["cpu_thresholds"]))
            self.gpu_row.set_bands(hw["gpu_thresholds"], active_band(hw["gpu_temp"], hw["gpu_thresholds"]))
        avg = f"{hw['avg_mhz'] / 1000:.2f}" if hw.get("avg_mhz") else "–"
        r["clock"].setText(f"{avg} / {hw['max_clock_mhz'] / 1000:.1f} GHz")
        r["power"].setText(f"{hw['pkg_w']:.0f} W" if hw.get("pkg_w") is not None else "–")
        self.defaults_label.setText(f"<small>My defaults: {self.describe(st['defaults'])}</small>")
        if st.get("failsafe"):
            self.show_error(st["failsafe"])
        elif st.get("errors"):
            self.show_error("; ".join(st["errors"]))


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("GT76 Fan Control")
    app.setDesktopFileName("gt76-fanctl")
    w = Window()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
