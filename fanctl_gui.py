#!/usr/bin/env python3
"""GT76 Fan Control: desktop UI for gt76-fanctld (fan curves, CPU clock cap, temperatures, light bars).

Runs as the normal user; all hardware changes go through the root service's socket.
"""
import html
import importlib.util
import json
import os
import socket
import sys

from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QColorDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QPushButton, QRadioButton, QSlider, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

SOCKET_PATH = os.environ.get("FANCTL_SOCKET", "/run/gt76-fanctl.sock")
CLOCK_MIN_MHZ = 1000
BANDS = 7


def _load_lightbar():
    """tools/lightbar.py: beside this file when installed, under tools/ in the repository."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (f"{here}/lightbar.py", f"{here}/tools/lightbar.py"):
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("lightbar", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise ImportError("lightbar.py not found")


lightbar = _load_lightbar()

# Light-bar presets: name, front-bar color stops (left to right), side-bar color.
LIGHT_PRESETS = [
    ("Red", ["ff0000"], "ff0000"),
    ("Ember", ["ff1800", "ff7000", "ff1800"], "ff3000"),
    ("Sunset", ["ff4000", "ff0058", "7000ff"], "ff4000"),
    ("Ocean", ["0018ff", "00b0ff", "0018ff"], "0050ff"),
    ("Aurora", ["00ff60", "00c0ff", "8000ff"], "00ff60"),
    ("Synthwave", ["ff00c0", "6000ff", "00e0ff"], "ff00c0"),
    ("Toxic", ["40ff00", "00ff60", "40ff00"], "40ff00"),
    ("Rainbow", ["ff0000", "ff8000", "ffff00", "00ff00", "0080ff", "8000ff"], "ffffff"),
    ("White", ["ffffff"], "ffffff"),
    ("Warm white", ["ff9a40"], "ff9a40"),
    ("Off", ["000000"], "000000"),
]


def hex_rgb(h):
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


def css_gradient(stops):
    if len(stops) == 1:
        return f"rgb({','.join(map(str, stops[0]))})"
    parts = ", ".join(f"stop:{i / (len(stops) - 1):.3f} rgb({','.join(map(str, c))})" for i, c in enumerate(stops))
    return f"qlineargradient(x1:0, y1:0, x2:1, y2:0, {parts})"


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


class LightPreview(QWidget):
    """Draws the 25 front-bar zones between the two side bars, as the controller will show them."""

    def __init__(self):
        super().__init__()
        self.front, self.sides, self.brightness = [(0, 0, 0)] * lightbar.FRONT_ZONES, (0, 0, 0), 255
        self.setMinimumHeight(54)

    def set_colors(self, front, sides, brightness):
        self.front, self.sides, self.brightness = front, sides, brightness
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        k = self.brightness / 255
        col = lambda c: QColor(*(round(v * k) for v in c))
        w, h = self.width(), self.height()
        side_w, gap = 18, 6
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(30, 30, 30))
        p.drawRoundedRect(QRectF(0, 0, w, h), 6, 6)
        for x in (gap, w - gap - side_w):
            p.setBrush(col(self.sides))
            p.drawRoundedRect(QRectF(x, gap, side_w, h - 2 * gap), 3, 3)
        x0, x1 = 2 * gap + side_w, w - 2 * gap - side_w
        zw = (x1 - x0) / len(self.front)
        for i, c in enumerate(self.front):
            p.setBrush(col(c))
            p.drawRect(QRectF(x0 + i * zw, h / 2 - 7, zw + 0.5, 14))


class LightsTab(QWidget):
    MAX_STOPS = 8

    def __init__(self, window):
        super().__init__()
        self.app_window = window
        self.stops, self.sides, self.name = [[255, 0, 0]], [255, 0, 0], "Custom"
        v = QVBoxLayout(self)

        presets = QGroupBox("Presets")
        g = QGridLayout(presets)
        for i, (name, stops, sides) in enumerate(LIGHT_PRESETS):
            b = QPushButton(name)
            b.setMinimumHeight(44)
            rgb = [hex_rgb(c) for c in stops]
            dark = sum(rgb[0]) < 200 and len(rgb) == 1
            b.setStyleSheet(f"QPushButton {{ background: {css_gradient(rgb)}; color: {'white' if dark else 'black'};"
                            " font-weight: bold; border-radius: 6px; border: 1px solid #555; }"
                            " QPushButton:hover { border: 2px solid palette(highlight); }")
            b.clicked.connect(lambda _, n=name, st=rgb, sd=hex_rgb(sides): self.choose(n, st, sd))
            g.addWidget(b, i // 4, i % 4)
        v.addWidget(presets)

        custom = QGroupBox("Custom")
        c = QVBoxLayout(custom)
        row = QHBoxLayout()
        row.addWidget(QLabel("Front bar, left to right:"))
        self.stop_box = QHBoxLayout()
        row.addLayout(self.stop_box)
        self.add_stop = QPushButton("+")
        self.add_stop.setToolTip("Add a color")
        self.add_stop.clicked.connect(lambda: self.edit_stops(+1))
        self.remove_stop = QPushButton("−")
        self.remove_stop.setToolTip("Remove the last color")
        self.remove_stop.clicked.connect(lambda: self.edit_stops(-1))
        for b in (self.add_stop, self.remove_stop):
            b.setFixedWidth(32)
            row.addWidget(b)
        row.addStretch()
        row.addWidget(QLabel("Side bars:"))
        self.sides_button = QPushButton()
        self.sides_button.setFixedSize(56, 30)
        self.sides_button.clicked.connect(self.pick_sides)
        row.addWidget(self.sides_button)
        c.addLayout(row)
        bright = QHBoxLayout()
        bright.addWidget(QLabel("Brightness:"))
        self.brightness = QSlider(Qt.Orientation.Horizontal)
        self.brightness.setRange(5, 255)
        self.brightness.setValue(255)
        self.bright_label = QLabel()
        self.bright_label.setMinimumWidth(44)
        self.brightness.valueChanged.connect(self.redraw)
        bright.addWidget(self.brightness, 1)
        bright.addWidget(self.bright_label)
        c.addLayout(bright)
        self.preview = LightPreview()
        c.addWidget(self.preview)
        apply = QPushButton("Apply colors")
        apply.clicked.connect(lambda: self.send())
        c.addWidget(apply, alignment=Qt.AlignmentFlag.AlignRight)
        v.addWidget(custom)

        self.now = QLabel()
        self.now.setWordWrap(True)
        v.addWidget(self.now)
        note = QLabel("Your choice is saved and restored at every boot and after sleep. The colors are not "
                      "written to the controller's own memory, so Windows keeps its own lighting.")
        note.setWordWrap(True)
        v.addWidget(note)
        v.addStretch()
        self.redraw()

    def swatch(self, button, rgb):
        button.setStyleSheet(f"background: rgb({','.join(map(str, rgb))}); border: 1px solid #555; border-radius: 4px;")

    def redraw(self):
        while self.stop_box.count():
            self.stop_box.takeAt(0).widget().deleteLater()
        for i, rgb in enumerate(self.stops):
            b = QPushButton()
            b.setFixedSize(44, 30)
            b.setToolTip("Click to change")
            self.swatch(b, rgb)
            b.clicked.connect(lambda _, i=i: self.pick_stop(i))
            self.stop_box.addWidget(b)
        self.add_stop.setEnabled(len(self.stops) < self.MAX_STOPS)
        self.remove_stop.setEnabled(len(self.stops) > 1)
        self.swatch(self.sides_button, self.sides)
        self.bright_label.setText(f"{round(self.brightness.value() / 255 * 100)} %")
        self.preview.set_colors(lightbar.gradient([tuple(c) for c in self.stops]), tuple(self.sides),
                                self.brightness.value())

    def pick(self, rgb, title):
        c = QColorDialog.getColor(QColor(*rgb), self, title)
        return [c.red(), c.green(), c.blue()] if c.isValid() else None

    def pick_stop(self, i):
        rgb = self.pick(self.stops[i], f"Front-bar color {i + 1}")
        if rgb:
            self.stops[i], self.name = rgb, "Custom"
            self.redraw()

    def pick_sides(self):
        rgb = self.pick(self.sides, "Side-bar color")
        if rgb:
            self.sides, self.name = rgb, "Custom"
            self.redraw()

    def edit_stops(self, d):
        if d > 0:
            self.stops.append(list(self.stops[-1]))
        else:
            self.stops.pop()
        self.name = "Custom"
        self.redraw()

    def choose(self, name, stops, sides):
        self.name, self.stops, self.sides = name, [list(c) for c in stops], list(sides)
        self.redraw()
        self.send()

    def send(self):
        lights = {"name": self.name, "stops": self.stops, "sides": self.sides,
                  "brightness": self.brightness.value()}
        self.app_window.send({"cmd": "lights", "lights": lights}, f"Light bars set to {self.name}.")

    def load(self, lights):
        if lights:
            self.name, self.stops, self.sides = lights["name"], [list(c) for c in lights["stops"]], list(lights["sides"])
            self.brightness.setValue(lights["brightness"])
            self.redraw()

    def show_status(self, st):
        l = st.get("lights")
        text = f"Now showing: <b>{html.escape(l['name'])}</b>" if l else "Not set yet: pick a preset."
        if st.get("lights_error"):
            text += f"  <span style='color:#c0392b'>{html.escape(st['lights_error'])}</span>"
        self.now.setText(text)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GT76 Fan Control")
        self.status = None
        self.loaded = False
        root = QWidget()
        outer = QVBoxLayout(root)
        self.setCentralWidget(root)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        fan_tab = QWidget()
        layout = QVBoxLayout(fan_tab)
        self.tabs.addTab(fan_tab, "Fans && clock")
        self.lights_tab = LightsTab(self)
        self.tabs.addTab(self.lights_tab, "Lights")

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
                      "If a custom curve lets the CPU reach 95 °C or the GPU 94 °C, the service "
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
        outer.addWidget(self.message)

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
        self.loaded_live = dict(live)

    def controls_unedited(self):
        """True if the controls still show the settings last loaded into them."""
        live = self.loaded_live
        return (self.clock_slider.value() == live["max_clock_mhz"] // 100
                and self.mode_adv.isChecked() == (live["fan_mode"] == "advanced")
                and self.cpu_row.values() == live["cpu_curve"] and self.gpu_row.values() == live["gpu_curve"])

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
        live = self.status["live"]
        if not self.loaded:
            self.load_controls(live)
            self.lights_tab.load(self.status.get("lights"))
            self.loaded = True
        elif live != self.loaded_live and self.controls_unedited():
            self.load_controls(live)  # changed elsewhere (another client or a script); follow it
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
        self.lights_tab.show_status(st)
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
