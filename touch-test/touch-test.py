#!/usr/bin/env python3
"""Fullscreen touchscreen test and calibration canvas.

Free draw mode
    Draw with a finger anywhere on the panel.  Every touch leaves an accent
    stroke and the header tracks the extreme coordinates reached, so dead
    edges and corners are easy to spot.  The canvas can be cleared without
    losing a calibration result, and a neighbouring *Result* button returns to
    it.  The draw log and extents are saved to
    ``~/.local/share/touch-test/last-draw.json``.

Calibrate mode
    Tap five targets (four corners and the centre).  Each tap is stored as a
    pair of "where the target was" and "where the touch was reported".  The
    app then fits a correction, composes it with the calibration matrix that
    libinput is currently using and writes a ready-to-use matrix.

    The correction is fitted in the panel's native physical pixel space and
    rotated through the output transform before being composed.  This matters
    on panels that run rotated (this tablet uses ``transform 90``); fitting
    directly in logical screen space would introduce a 90 degree error.

    Nothing is applied automatically.  The result screen has an *Apply*
    button (it runs ``swaymsg input ... calibration_matrix`` and writes
    ``~/.config/sway/local-touch-calibration.conf`` so the value survives a
    reboot) and a *Revert* button that restores the previous matrix.

Run it through the ``touch-test`` wrapper so waybar is hidden for the
duration of the test; the panel is otherwise under waybar's exclusive zone.
"""

import datetime
import glob
import json
import math
import os
import subprocess
import sys
import time

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo

CORNER = 96          # size of the corner hit zones (logical px)
GRID_STEP = 100      # background grid spacing
INK = 6.0            # stroke width
MAX_POINTS = 50000   # cap on the saved free-draw log
BG = (0.118, 0.118, 0.157)
GRID_COL = (0.20, 0.20, 0.26)
TEXT = (0.804, 0.839, 0.957)
MUTED = (0.54, 0.57, 0.67)

DATA_DIR = os.path.expanduser("~/.local/share/touch-test")
SWAY_CALIBRATION_FILE = os.path.expanduser(
    "~/.config/sway/local-touch-calibration.conf")

# Targets are fractions of the logical screen.  They avoid the top status
# panel and the bottom toolbar (see Toolbar layout below).
TARGETS = [(0.10, 0.10), (0.90, 0.10), (0.90, 0.90),
           (0.10, 0.90), (0.50, 0.50)]

# Sway / wlroots output transform names.
TRANSFORM_NAMES = {
    "normal": 0,
    "90": 1,
    "180": 2,
    "270": 3,
    "flipped": 4,
    "flipped-90": 5,
    "flipped-180": 6,
    "flipped-270": 7,
}


def _rgb(h):
    h = h.lstrip("#")
    try:
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except (ValueError, IndexError):
        return (0.55, 0.15, 0.18)


def load_accent():
    try:
        with open(os.path.expanduser("~/.config/sway/accent")) as f:
            h = f.read().split()[0]
    except (OSError, IndexError):
        h = "#8c272e"
    if not h.startswith("#"):
        h = "#" + h
    if len(h) != 7:
        h = "#8c272e"
    return h


ACCENT_HEX = load_accent()
ACCENT = _rgb(ACCENT_HEX)

CSS = ("""
window, label, button {
  font-family: "Berkeley Mono", "IBM Plex Mono", "Noto Sans Mono",
               "DejaVu Sans Mono", monospace;
}
window { background-color: #1e1e2e; color: #cdd6f4; }
.toolbar {
  background-color: rgba(30, 30, 46, 0.90);
  border: 1px solid rgba(205, 214, 244, 0.18);
  border-radius: 6px;
  padding: 8px;
}
button {
  background-image: none;
  background-color: #313244;
  color: #cdd6f4;
  border: 1px solid rgba(205, 214, 244, 0.16);
  border-radius: 4px;
  padding: 10px 20px;
  font-size: 15px;
}
button:hover { background-color: #45475a; }
button:active { background-color: %s; color: #ffffff; }
button:disabled { color: rgba(205, 214, 244, 0.35); }
""" % ACCENT_HEX).encode()


# ---- calibration maths ----------------------------------------------------

def solve3(matrix, vector):
    """Solve a 3x3 linear system with partial pivoting."""
    m = [list(matrix[i]) + [vector[i]] for i in range(3)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            return [0.0, 0.0, 0.0]
        m[col], m[pivot] = m[pivot], m[col]
        div = m[col][col]
        for j in range(col, 4):
            m[col][j] /= div
        for row in range(3):
            if row == col:
                continue
            factor = m[row][col]
            if factor:
                for j in range(col, 4):
                    m[row][j] -= factor * m[col][j]
    return [m[i][3] for i in range(3)]


def fit_affine(src, dst):
    """Least-squares affine map from src points to dst points."""
    n = len(src)
    sxx = sum(x * x for x, _y in src)
    sxy = sum(x * y for x, y in src)
    syy = sum(y * y for _x, y in src)
    sx = sum(x for x, _y in src)
    sy = sum(y for _x, y in src)
    m = [[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, float(n)]]
    bx = [sum(x * dx for (x, _y), (dx, _dy) in zip(src, dst)),
          sum(y * dx for (_x, y), (dx, _dy) in zip(src, dst)),
          sum(dx for dx, _dy in dst)]
    by = [sum(x * dy for (x, _y), (_dx, dy) in zip(src, dst)),
          sum(y * dy for (_x, y), (_dx, dy) in zip(src, dst)),
          sum(dy for _dx, dy in dst)]
    ax = solve3(m, bx)
    ay = solve3(m, by)
    return [[ax[0], ax[1], ax[2]],
            [ay[0], ay[1], ay[2]],
            [0.0, 0.0, 1.0]]


def matmul3(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def matvec3(m, x, y):
    return (m[0][0] * x + m[0][1] * y + m[0][2],
            m[1][0] * x + m[1][1] * y + m[1][2])


def flatten3(m):
    return [m[0][0], m[0][1], m[0][2], m[1][0], m[1][1], m[1][2]]


def matrix3(values):
    return [[values[0], values[1], values[2]],
            [values[3], values[4], values[5]],
            [0.0, 0.0, 1.0]]


def fmt_matrix(values):
    return " ".join("%.9f" % v for v in values)


def transform_point(x, y, transform):
    """Apply the wlroots output transform to normalised coordinates."""
    if transform == 0:
        return x, y
    if transform == 1:
        return 1.0 - y, x
    if transform == 2:
        return 1.0 - x, 1.0 - y
    if transform == 3:
        return y, 1.0 - x
    if transform == 4:
        return 1.0 - x, y
    if transform == 5:
        return y, x
    if transform == 6:
        return x, 1.0 - y
    if transform == 7:
        return 1.0 - y, 1.0 - x
    return x, y


def inverse_transform_point(x, y, transform):
    """Inverse of :func:`transform_point` for the eight output transforms."""
    if transform == 0:
        return x, y
    if transform == 1:
        return y, 1.0 - x
    if transform == 2:
        return 1.0 - x, 1.0 - y
    if transform == 3:
        return 1.0 - y, x
    if transform == 4:
        return 1.0 - x, y
    if transform == 5:
        return y, x
    if transform == 6:
        return x, 1.0 - y
    if transform == 7:
        return 1.0 - y, 1.0 - x
    return x, y


def parse_transform(value):
    if isinstance(value, int):
        return max(0, min(7, value))
    if isinstance(value, str):
        if value in TRANSFORM_NAMES:
            return TRANSFORM_NAMES[value]
        try:
            return max(0, min(7, int(value)))
        except ValueError:
            pass
    return 0


def sway_environ():
    env = dict(os.environ)
    runtime = env.get("XDG_RUNTIME_DIR",
                      "/run/user/%d" % os.getuid())
    if "SWAYSOCK" not in env:
        sockets = sorted(glob.glob(os.path.join(runtime, "sway-ipc.*.sock")))
        if sockets:
            env["SWAYSOCK"] = sockets[-1]
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    return env


def sway_json(kind):
    try:
        out = subprocess.check_output(
            ["swaymsg", "-t", kind, "-r"],
            env=sway_environ(), stderr=subprocess.DEVNULL,
            text=True, timeout=3)
        return json.loads(out)
    except Exception:
        return None


def get_touch_device():
    """Return (identifier, current matrix, output info)."""
    inputs = sway_json("get_inputs")
    if inputs is None:
        return None, None, None
    device_id = None
    matrix = None
    for dev in inputs:
        if dev.get("type") != "touch":
            continue
        device_id = dev.get("identifier")
        matrix = (dev.get("libinput") or {}).get("calibration_matrix")
        if matrix is not None:
            try:
                matrix = [float(v) for v in matrix]
            except (TypeError, ValueError):
                matrix = None
        break

    output = None
    outputs = sway_json("get_outputs")
    if outputs:
        chosen = None
        for candidate in outputs:
            if candidate.get("focused") and candidate.get("active"):
                chosen = candidate
                break
        if chosen is None:
            for candidate in outputs:
                if candidate.get("active"):
                    chosen = candidate
                    break
        if chosen is None:
            chosen = outputs[0]
        mode = chosen.get("current_mode") or {}
        rect = chosen.get("rect") or {}
        output = {
            "name": chosen.get("name"),
            "transform": parse_transform(chosen.get("transform", "normal")),
            "logical_w": rect.get("width") or 0,
            "logical_h": rect.get("height") or 0,
            "phys_w": mode.get("width") or rect.get("height") or 0,
            "phys_h": mode.get("height") or rect.get("width") or 0,
        }
    return device_id, matrix, output


def _short_error(message):
    return " ".join(str(message).split())[:120]


class TouchTest(Gtk.Window):
    def __init__(self):
        super().__init__(title="Touch test")
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_app_paintable(True)
        self.fullscreen()
        try:
            self.set_role("org.swaytablet.touchtest")
        except Exception:
            pass
        self.connect("key-press-event", self.on_key)
        self.connect("destroy", self.on_destroy)

        self.w = 0
        self.h = 0
        self.surface = None
        self.cur = None
        self.min_x = self.min_y = None
        self.max_x = self.max_y = None
        self.corners = {"TL": False, "TR": False, "BL": False, "BR": False}
        self.pointer_down = False
        self.pointer_last = None
        self.touching = False
        self.touch_last = {}
        self.last_touch = 0.0

        self.device_id, self.current_matrix, self.output = get_touch_device()
        self.transform = (self.output or {}).get("transform", 0)
        self.phys_w = (self.output or {}).get("phys_w") or 0
        self.phys_h = (self.output or {}).get("phys_h") or 0

        self.mode = "test"          # test | calibrate | result
        self.calib_index = 0
        self.samples = []
        self.correction = None      # 3x3, physical pixel space
        self.correction_norm = None # 3x3, physical normalised space
        self.new_matrix = None
        self.composed = False
        self.mean_error = None
        self.max_error = None
        self.save_error = ""
        self.apply_status = ""
        self.applied_previous = None
        self.calib_ready_time = 0.0
        self._calib_tap = None

        self.strokes = []
        self.draw_points = 0
        self.active_stroke = None
        self.quitting = False

        self.area = Gtk.DrawingArea()
        self.area.set_can_focus(True)
        self.area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.TOUCH_MASK)
        self.area.connect("draw", self.on_draw)
        self.area.connect("configure-event", self.on_configure)
        self.area.connect("button-press-event", self.on_press)
        self.area.connect("motion-notify-event", self.on_motion)
        self.area.connect("button-release-event", self.on_release)
        self.area.connect("event", self.on_event)
        self.area.connect("realize", lambda *a: self.area.grab_focus())

        overlay = Gtk.Overlay()
        overlay.add(self.area)

        self.bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.bar.get_style_context().add_class("toolbar")
        self.bar.set_halign(Gtk.Align.CENTER)
        self.bar.set_valign(Gtk.Align.END)
        self.bar.set_margin_bottom(14)
        overlay.add_overlay(self.bar)
        self._sync_toolbar()

        self.add(overlay)

    # ---- toolbar ---------------------------------------------------------
    def _button(self, label, callback):
        button = Gtk.Button(label=label)
        button.connect("clicked", lambda *_a: callback())
        return button

    def _sync_toolbar(self):
        for child in self.bar.get_children():
            self.bar.remove(child)
        if self.mode == "test":
            self.bar.pack_start(self._button("Clear", self.clear_canvas),
                                False, False, 0)
            if self.samples or self.new_matrix is not None:
                self.bar.pack_start(self._button("Result", self.show_result),
                                    False, False, 0)
            self.bar.pack_start(self._button("Calibrate", self.start_calibration),
                                False, False, 0)
        elif self.mode == "calibrate":
            self.bar.pack_start(self._button("Free draw", self.start_draw),
                                False, False, 0)
        else:
            self.bar.pack_start(self._button("Free draw", self.start_draw),
                                False, False, 0)
            self.bar.pack_start(self._button("Again", self.start_calibration),
                                False, False, 0)
            if self.new_matrix is not None:
                if self.applied_previous is not None:
                    self.bar.pack_start(self._button("Revert", self.revert),
                                        False, False, 0)
                else:
                    self.bar.pack_start(self._button("Apply", self.apply),
                                        False, False, 0)
        self.bar.pack_start(self._button("Quit", self.quit), False, False, 0)
        self.bar.show_all()

    # ---- canvas ---------------------------------------------------------
    def _new_surface(self, w, h):
        self.w, self.h = max(1, int(w)), max(1, int(h))
        if not self.phys_w or not self.phys_h:
            self.phys_w, self.phys_h = self.w, self.h
        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.w, self.h)
        cr = cairo.Context(self.surface)
        cr.set_source_rgb(*BG)
        cr.paint()
        cr.set_line_width(1)
        cr.set_source_rgba(*GRID_COL, 0.9)
        for x in range(0, self.w, GRID_STEP):
            cr.move_to(x + 0.5, 0)
            cr.line_to(x + 0.5, self.h)
        for y in range(0, self.h, GRID_STEP):
            cr.move_to(0, y + 0.5)
            cr.line_to(self.w, y + 0.5)
        cr.stroke()
        # a 10 px inset reference frame, so the real edge is easy to judge
        cr.set_source_rgba(*MUTED, 0.35)
        cr.rectangle(9.5, 9.5, self.w - 19, self.h - 19)
        cr.stroke()

    def on_configure(self, _w, event):
        if event.width != self.w or event.height != self.h:
            self._new_surface(event.width, event.height)
        return False

    def on_draw(self, widget, cr):
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        if self.surface is None or w != self.w or h != self.h:
            self._new_surface(w, h)
        cr.set_source_surface(self.surface, 0, 0)
        cr.paint()
        self._draw_hud(cr, w, h)
        return False

    def _stroke(self, last, x, y):
        if self.surface is None:
            return
        cr = cairo.Context(self.surface)
        cr.set_source_rgb(*ACCENT)
        cr.set_line_width(INK)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        if last is None:
            cr.arc(x, y, INK * 0.8, 0, 2 * math.pi)
            cr.fill()
        else:
            cr.move_to(last[0], last[1])
            cr.line_to(x, y)
            cr.stroke()
        self._mark(x, y)
        self.area.queue_draw()

    def _mark(self, x, y):
        self.cur = (x, y)
        self.min_x = x if self.min_x is None else min(self.min_x, x)
        self.min_y = y if self.min_y is None else min(self.min_y, y)
        self.max_x = x if self.max_x is None else max(self.max_x, x)
        self.max_y = y if self.max_y is None else max(self.max_y, y)
        if x <= CORNER and y <= CORNER:
            self.corners["TL"] = True
        if x >= self.w - CORNER and y <= CORNER:
            self.corners["TR"] = True
        if x <= CORNER and y >= self.h - CORNER:
            self.corners["BL"] = True
        if x >= self.w - CORNER and y >= self.h - CORNER:
            self.corners["BR"] = True

    # ---- draw-log -------------------------------------------------------
    def _log_point(self, x, y):
        if self.active_stroke is None or self.draw_points >= MAX_POINTS:
            return
        self.active_stroke.append([round(x, 1), round(y, 1)])
        self.draw_points += 1

    def _begin_log(self, x, y):
        if self.draw_points >= MAX_POINTS:
            return
        self.active_stroke = []
        self.strokes.append(self.active_stroke)
        self._log_point(x, y)

    def _save_draw_log(self):
        if not self.strokes:
            return
        if self.device_id is None:
            self.device_id, _matrix, _output = get_touch_device()
        data = {
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "screen": [self.w, self.h],
            "physical_screen": [self.phys_w, self.phys_h],
            "transform": self.transform,
            "device": self.device_id,
            "mode": self.mode,
            "extents": {
                "left": self.min_x,
                "right": self.max_x,
                "top": self.min_y,
                "bottom": self.max_y,
            },
            "strokes": self.strokes,
        }
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(os.path.join(DATA_DIR, "last-draw.json"), "w") as f:
                json.dump(data, f)
        except OSError:
            pass

    # ---- pointer (mouse and GTK touch emulation) ------------------------
    def on_press(self, _w, event):
        if event.button != 1:
            return False
        if self.touching or time.monotonic() - self.last_touch < 0.3:
            return True
        if self.mode == "calibrate":
            self._record_calibration(event.x, event.y, "pointer")
            return True
        if self.mode != "test":
            return True
        self.pointer_down = True
        self.pointer_last = (event.x, event.y)
        self._begin_log(event.x, event.y)
        self._stroke(None, event.x, event.y)
        return True

    def on_motion(self, _w, event):
        if not self.pointer_down:
            return False
        if self.mode != "test":
            return True
        if self.touching or time.monotonic() - self.last_touch < 0.3:
            return True
        self._log_point(event.x, event.y)
        self._stroke(self.pointer_last, event.x, event.y)
        self.pointer_last = (event.x, event.y)
        return True

    def on_release(self, _w, event):
        if event.button == 1:
            self.pointer_down = False
            self.pointer_last = None
            if not self.touching:
                self.active_stroke = None
                self._save_draw_log()
        return False

    # ---- native touch events (multi-finger capable) ---------------------
    def on_event(self, _w, event):
        et = event.type
        if et not in (Gdk.EventType.TOUCH_BEGIN, Gdk.EventType.TOUCH_UPDATE,
                      Gdk.EventType.TOUCH_END, Gdk.EventType.TOUCH_CANCEL):
            return False
        self.last_touch = time.monotonic()
        t = event.touch
        try:
            key = hash(t.sequence)
        except TypeError:
            key = 0
        if et == Gdk.EventType.TOUCH_BEGIN:
            self.touching = True
            if self.mode == "calibrate":
                self._record_calibration(t.x, t.y, "touch")
                return True
            if self.mode != "test":
                return True
            self.touch_last[key] = (t.x, t.y)
            self._begin_log(t.x, t.y)
            self._stroke(None, t.x, t.y)
        elif et == Gdk.EventType.TOUCH_UPDATE:
            if self.mode != "test":
                return True
            self.touching = True
            prev = self.touch_last.get(key)
            self._log_point(t.x, t.y)
            self._stroke(prev, t.x, t.y)
            self.touch_last[key] = (t.x, t.y)
        else:
            self.touch_last.pop(key, None)
            if not self.touch_last:
                self.touching = False
                if self.mode == "test":
                    self.active_stroke = None
                    self._save_draw_log()
        return True

    # ---- calibration flow ----------------------------------------------
    def _target(self, index):
        fx, fy = TARGETS[index]
        return fx * (self.w or 1), fy * (self.h or 1)

    def start_calibration(self):
        self.mode = "calibrate"
        self.calib_index = 0
        self.samples = []
        self.correction = None
        self.correction_norm = None
        self.new_matrix = None
        self.composed = False
        self.mean_error = None
        self.max_error = None
        self.save_error = ""
        self.apply_status = ""
        self.applied_previous = None
        self._calib_tap = None
        self.calib_ready_time = 0.0
        self.device_id, self.current_matrix, self.output = get_touch_device()
        self.transform = (self.output or {}).get("transform", 0)
        self.phys_w = (self.output or {}).get("phys_w") or self.w
        self.phys_h = (self.output or {}).get("phys_h") or self.h
        self._sync_toolbar()
        self._save_draw_log()
        if self.w > 1 and self.h > 1:
            self._new_surface(self.w, self.h)
        self.cur = None
        self.min_x = self.min_y = None
        self.max_x = self.max_y = None
        self.corners = {name: False for name in self.corners}
        self.pointer_down = False
        self.pointer_last = None
        self.touching = False
        self.touch_last.clear()
        self.active_stroke = None
        self.strokes = []
        self.draw_points = 0
        self.area.queue_draw()

    def _record_calibration(self, x, y, source):
        if self.mode != "calibrate" or self.calib_index >= len(TARGETS):
            return
        now = time.monotonic()
        if now < self.calib_ready_time:
            return
        if self._calib_tap is not None:
            stamp, last_x, last_y = self._calib_tap
            if now - stamp < 0.4 and math.hypot(x - last_x, y - last_y) < 40:
                return
        tx, ty = self._target(self.calib_index)
        self.samples.append({
            "target": [tx, ty],
            "observed": [x, y],
            "source": source,
        })
        self._calib_tap = (now, x, y)
        self.calib_ready_time = now + 0.4
        self.cur = (x, y)
        self.calib_index += 1
        if self.calib_index >= len(TARGETS):
            self._finish_calibration()
        self.area.queue_draw()

    def _finish_calibration(self):
        self.mode = "result"
        w = max(1, self.w)
        h = max(1, self.h)
        pw = max(1, self.phys_w)
        ph = max(1, self.phys_h)
        transform = self.transform

        src_px = []
        dst_px = []
        for sample in self.samples:
            ox, oy = sample["observed"]
            tx, ty = sample["target"]
            # Logical normalised coordinates from the app window.
            onx, ony = ox / w, oy / h
            tnx, tny = tx / w, ty / h
            # Undo the compositor's output transform so the fit happens in
            # the panel's native physical space.
            opx, opy = inverse_transform_point(onx, ony, transform)
            tpx, tpy = inverse_transform_point(tnx, tny, transform)
            src_px.append((opx * pw, opy * ph))
            dst_px.append((tpx * pw, tpy * ph))

        correction_px = fit_affine(src_px, dst_px)
        self.correction = correction_px

        # Convert the pixel-space correction into physical normalised space:
        # C_norm = S^-1 * C_px * S with S = diag(pw, ph, 1).
        scale = [[pw, 0.0, 0.0], [0.0, ph, 0.0], [0.0, 0.0, 1.0]]
        inv_scale = [[1.0 / pw, 0.0, 0.0], [0.0, 1.0 / ph, 0.0],
                     [0.0, 0.0, 1.0]]
        self.correction_norm = matmul3(
            matmul3(inv_scale, correction_px), scale)

        errors = []
        for (sx, sy), (dx, dy) in zip(src_px, dst_px):
            px, py = matvec3(correction_px, sx, sy)
            errors.append(math.hypot(px - dx, py - dy))
        self.mean_error = sum(errors) / len(errors) if errors else 0.0
        self.max_error = max(errors) if errors else 0.0

        if self.current_matrix is not None:
            # M' = C_norm * M, because C_norm expects physical normalised
            # coordinates and libinput's existing matrix produces them.
            self.new_matrix = flatten3(matmul3(
                self.correction_norm, matrix3(self.current_matrix)))
            self.composed = True
        else:
            # No existing matrix: libinput is using identity, so the
            # correction itself is the absolute matrix.
            self.new_matrix = flatten3(self.correction_norm)
            self.composed = False

        self._save_calibration()
        self._sync_toolbar()
        self.area.queue_draw()

    def _save_calibration(self):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        samples = []
        for sample in self.samples:
            ox, oy = sample["observed"]
            tx, ty = sample["target"]
            samples.append({
                "target": [round(tx, 2), round(ty, 2)],
                "observed": [round(ox, 2), round(oy, 2)],
                "source": sample["source"],
            })
        data = {
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "logical_screen": [self.w, self.h],
            "physical_screen": [self.phys_w, self.phys_h],
            "transform": self.transform,
            "device": self.device_id,
            "current_matrix": self.current_matrix,
            "samples": samples,
            "correction_px": self.correction,
            "correction_normalised": self.correction_norm,
            "new_matrix": self.new_matrix,
            "composed_with_current": self.composed,
            "mean_error_px": round(self.mean_error or 0.0, 2),
            "max_error_px": round(self.max_error or 0.0, 2),
        }
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            text = json.dumps(data, indent=2)
            for name in ("calibration-%s.json" % stamp, "latest.json"):
                with open(os.path.join(DATA_DIR, name), "w") as f:
                    f.write(text)
            if self.new_matrix is not None:
                self._write_matrix_files(self.new_matrix)
                if self.current_matrix is not None:
                    with open(os.path.join(DATA_DIR, "revert-matrix.txt"),
                              "w") as f:
                        f.write(fmt_matrix(self.current_matrix) + "\n")
                    with open(os.path.join(DATA_DIR, "revert-command.txt"),
                              "w") as f:
                        f.write('swaymsg input "%s" calibration_matrix %s\n'
                                % (self.device_id,
                                   fmt_matrix(self.current_matrix)))
        except OSError as exc:
            self.save_error = _short_error(exc)

    def _write_matrix_files(self, values):
        os.makedirs(DATA_DIR, exist_ok=True)
        matrix_text = fmt_matrix(values)
        with open(os.path.join(DATA_DIR, "latest-matrix.txt"), "w") as f:
            f.write(matrix_text + "\n")
        conf = (
            "# Generated by touch-test on %s.\n"
            "# Loaded by the existing include local*.conf in "
            "~/.config/sway/config.\n"
            'input "%s" {\n'
            "    calibration_matrix %s\n"
            "}\n"
        ) % (datetime.datetime.now().isoformat(timespec="seconds"),
             self.device_id, matrix_text)
        with open(os.path.join(DATA_DIR, "latest-apply.conf"), "w") as f:
            f.write(conf)
        with open(os.path.join(DATA_DIR, "latest-command.txt"), "w") as f:
            f.write('swaymsg input "%s" calibration_matrix %s\n'
                    % (self.device_id, matrix_text))

    def _run_sway_input(self, values):
        if not self.device_id:
            return False, "touch device not found"
        command = ["swaymsg", "input", self.device_id, "calibration_matrix"]
        command.extend("%.9f" % v for v in values)
        try:
            result = subprocess.run(
                command, env=sway_environ(), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=5)
        except Exception as exc:
            return False, _short_error(exc)
        if result.returncode != 0:
            return False, _short_error(result.stdout or "swaymsg failed")
        return True, ""

    def apply(self):
        if self.new_matrix is None:
            self.apply_status = "nothing to apply"
            self.area.queue_draw()
            return
        previous = list(self.current_matrix) if self.current_matrix else None
        ok, error = self._run_sway_input(self.new_matrix)
        if not ok:
            self.apply_status = "apply failed: " + error
            self.area.queue_draw()
            return
        try:
            os.makedirs(os.path.dirname(SWAY_CALIBRATION_FILE), exist_ok=True)
            with open(SWAY_CALIBRATION_FILE, "w") as f:
                f.write(
                    "# Generated by touch-test on %s.\n"
                    "# Loaded by the existing include local*.conf in "
                    "~/.config/sway/config.\n"
                    'input "%s" {\n'
                    "    calibration_matrix %s\n"
                    "}\n"
                    % (datetime.datetime.now().isoformat(timespec="seconds"),
                       self.device_id, fmt_matrix(self.new_matrix)))
        except OSError as exc:
            self.apply_status = "applied, save failed: " + _short_error(exc)
            self.applied_previous = previous
            self._sync_toolbar()
            self.area.queue_draw()
            return
        self.applied_previous = previous
        self.apply_status = "applied and saved for next login"
        self._sync_toolbar()
        self.area.queue_draw()

    def revert(self):
        if self.applied_previous is None:
            self.apply_status = "nothing to revert"
            self.area.queue_draw()
            return
        ok, error = self._run_sway_input(self.applied_previous)
        if not ok:
            self.apply_status = "revert failed: " + error
            self.area.queue_draw()
            return
        try:
            os.unlink(SWAY_CALIBRATION_FILE)
        except OSError:
            pass
        self.applied_previous = None
        self.apply_status = "reverted to the previous matrix"
        self._sync_toolbar()
        self.area.queue_draw()

    # ---- HUD ------------------------------------------------------------
    @staticmethod
    def _v(value):
        return "-" if value is None else str(int(round(value)))

    @staticmethod
    def _layout(cr, text, size):
        layout = PangoCairo.create_layout(cr)
        layout.set_text(text, -1)
        desc = Pango.FontDescription()
        desc.set_family("monospace")
        desc.set_size(int(size * Pango.SCALE))
        layout.set_font_description(desc)
        return layout

    @staticmethod
    def _rounded(cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.arc(x + w - r, y + r, r, 1.5 * math.pi, 2 * math.pi)
        cr.arc(x + w - r, y + h - r, r, 0, 0.5 * math.pi)
        cr.arc(x + r, y + h - r, r, 0.5 * math.pi, math.pi)
        cr.close_path()

    def _text(self, cr, x, y, text, size=13, color=TEXT, alpha=1.0,
              center_x=False):
        layout = self._layout(cr, text, size)
        tw, th = layout.get_pixel_size()
        if center_x:
            x -= tw / 2.0
        cr.set_source_rgba(color[0], color[1], color[2], alpha)
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, layout)
        return tw, th

    def _hud_lines(self, w, h):
        if self.mode == "calibrate":
            return [
                "calibrate   tap the ring   (%d/%d)"
                % (min(self.calib_index + 1, len(TARGETS)), len(TARGETS)),
                "device %s   screen %dx%d   transform %d   panel %dx%d"
                % (self.device_id or "unknown", w, h, self.transform,
                   self.phys_w, self.phys_h),
                "each tap is saved for recalibration",
            ]
        if self.mode == "result":
            if self.new_matrix:
                matrix = "new matrix  " + fmt_matrix(self.new_matrix)
            else:
                matrix = "current matrix unknown - samples saved"
            lines = [
                "calibration saved   %d samples   mean %.1f px   max %.1f px"
                % (len(self.samples), self.mean_error or 0.0,
                   self.max_error or 0.0),
                matrix,
                "file  ~/.local/share/touch-test/latest.json",
            ]
            if self.save_error:
                lines.append("save error: " + self.save_error)
            if self.apply_status:
                lines.append(self.apply_status)
            return lines
        cur = self.cur
        cx = "-" if cur is None else str(int(round(cur[0])))
        cy = "-" if cur is None else str(int(round(cur[1])))
        lines = [
            "free draw   canvas %dx%d    x %s    y %s"
            % (w, h, cx, cy),
            "reached   left %s   right %s   top %s   bottom %s" % (
                self._v(self.min_x), self._v(self.max_x),
                self._v(self.min_y), self._v(self.max_y)),
            "draw anywhere with a finger  |  Clear resets the canvas",
        ]
        if self.samples or self.new_matrix is not None:
            lines.append("calibration result kept  |  Result returns to it")
        return lines

    def _panel(self, cr, w, lines):
        layouts = [self._layout(cr, text, 13) for text in lines]
        sizes = [layout.get_pixel_size() for layout in layouts]
        line_h = max(size[1] for size in sizes)
        width = max(size[0] for size in sizes)
        height = line_h * len(lines) + 6 * (len(lines) - 1) + 20
        px = (w - width) / 2.0 - 10
        py = 10
        self._rounded(cr, px, py, width + 20, height, 6)
        cr.set_source_rgba(0.10, 0.10, 0.15, 0.82)
        cr.fill_preserve()
        cr.set_source_rgba(*MUTED, 0.35)
        cr.set_line_width(1)
        cr.stroke()
        ty = py + 10
        for layout in layouts:
            cr.set_source_rgba(*TEXT, 0.96)
            cr.move_to(px + 10, ty)
            PangoCairo.show_layout(cr, layout)
            ty += line_h + 6

    def _draw_hud(self, cr, w, h):
        # outer frame: the actual screen edge
        cr.set_line_width(2)
        cr.set_source_rgba(*MUTED, 0.5)
        cr.rectangle(1, 1, w - 2, h - 2)
        cr.stroke()

        self._panel(cr, w, self._hud_lines(w, h))

        if self.mode == "test":
            for name in ("TL", "TR", "BL", "BR"):
                self._corner(cr, w, h, name)
            if self.cur is not None:
                self._crosshair(cr, w, h)
            return

        self._draw_samples(cr)
        if self.mode == "calibrate":
            self._draw_target(cr)

    def _crosshair(self, cr, w, h):
        x, y = self.cur
        alpha = 0.9 if (self.touching or self.pointer_down) else 0.45
        cr.set_source_rgba(*ACCENT, alpha * 0.35)
        cr.set_line_width(1)
        cr.move_to(x + 0.5, 0)
        cr.line_to(x + 0.5, h)
        cr.move_to(0, y + 0.5)
        cr.line_to(w, y + 0.5)
        cr.stroke()
        cr.set_source_rgba(*ACCENT, alpha)
        cr.set_line_width(2)
        cr.arc(x, y, 10, 0, 2 * math.pi)
        cr.stroke()

    def _corner(self, cr, w, h, name):
        hit = self.corners[name]
        col = ACCENT if hit else MUTED
        alpha = 1.0 if hit else 0.5
        cr.set_line_width(3 if hit else 2)
        cr.set_source_rgba(*col, alpha)
        o, length = 8, 48
        if name == "TL":
            cr.move_to(o, o + length)
            cr.line_to(o, o)
            cr.line_to(o + length, o)
            tx, ty = o + 6, o + 6
        elif name == "TR":
            cr.move_to(w - o - length, o)
            cr.line_to(w - o, o)
            cr.line_to(w - o, o + length)
            tx, ty = None, o + 6
        elif name == "BL":
            cr.move_to(o, h - o - length)
            cr.line_to(o, h - o)
            cr.line_to(o + length, h - o)
            tx, ty = o + 6, None
        else:
            cr.move_to(w - o - length, h - o)
            cr.line_to(w - o, h - o)
            cr.line_to(w - o, h - o - length)
            tx, ty = None, None
        cr.stroke()

        layout = self._layout(cr, "%s %s" % (name, "[+]" if hit else "[-]"), 13)
        tw, th = layout.get_pixel_size()
        if tx is None:
            tx = w - o - 6 - tw
        if ty is None:
            ty = h - o - 6 - th
        cr.set_source_rgba(*col, alpha)
        cr.move_to(tx, ty)
        PangoCairo.show_layout(cr, layout)

    def _draw_samples(self, cr):
        for index, sample in enumerate(self.samples):
            tx, ty = sample["target"]
            ox, oy = sample["observed"]
            cr.set_source_rgba(*MUTED, 0.5)
            cr.set_line_width(1)
            cr.move_to(tx, ty)
            cr.line_to(ox, oy)
            cr.stroke()
            cr.set_source_rgba(*TEXT, 0.32)
            cr.set_line_width(1)
            cr.arc(tx, ty, 12, 0, 2 * math.pi)
            cr.stroke()
            cr.set_source_rgba(*ACCENT, 0.95)
            cr.arc(ox, oy, 5, 0, 2 * math.pi)
            cr.fill()
            self._text(cr, tx + 16, ty + 8, str(index + 1), 11, MUTED, 0.8)

    def _draw_target(self, cr):
        if self.calib_index >= len(TARGETS):
            return
        x, y = self._target(self.calib_index)
        cr.set_source_rgba(*ACCENT, 0.95)
        cr.set_line_width(3)
        cr.arc(x, y, 32, 0, 2 * math.pi)
        cr.stroke()
        cr.set_line_width(1)
        cr.arc(x, y, 46, 0, 2 * math.pi)
        cr.stroke()
        cr.arc(x, y, 4, 0, 2 * math.pi)
        cr.fill()
        for x1, y1, x2, y2 in (
                (x - 52, y, x - 14, y), (x + 14, y, x + 52, y),
                (x, y - 52, x, y - 14), (x, y + 14, x, y + 52)):
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
        cr.stroke()
        self._text(cr, x, y - 74,
                   "tap here   %d/%d" % (self.calib_index + 1, len(TARGETS)),
                   13, TEXT, 0.95, center_x=True)

    # ---- free draw ------------------------------------------------------
    def _reset_canvas(self):
        if self.w > 1 and self.h > 1:
            self._new_surface(self.w, self.h)
        self.cur = None
        self.min_x = self.min_y = None
        self.max_x = self.max_y = None
        self.corners = {name: False for name in self.corners}
        self.pointer_down = False
        self.pointer_last = None
        self.touching = False
        self.touch_last.clear()
        self.active_stroke = None
        self.strokes = []
        self.draw_points = 0
        self.area.queue_draw()

    def start_draw(self):
        """Enter free draw mode, keeping any calibration result intact."""
        self.mode = "test"
        self._reset_canvas()
        self._sync_toolbar()

    def clear_canvas(self):
        """Clear the drawing without touching calibration data."""
        if self.mode == "test":
            self._reset_canvas()
        else:
            self.start_draw()

    def show_result(self):
        """Return to the calibration result after trying a free draw."""
        self.mode = "result"
        self._sync_toolbar()
        self.area.queue_draw()

    def clear(self):
        """Compatibility alias: start a fresh free draw."""
        self.start_draw()

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        self._save_draw_log()
        Gtk.main_quit()

    def on_destroy(self, *_args):
        self._save_draw_log()
        Gtk.main_quit()

    def on_key(self, _w, event):
        if event.keyval == Gdk.KEY_Escape:
            self.quit()
            return True
        if event.keyval in (Gdk.KEY_c, Gdk.KEY_C):
            self.clear_canvas()
            return True
        if event.keyval in (Gdk.KEY_d, Gdk.KEY_D):
            self.start_draw()
            return True
        if event.keyval in (Gdk.KEY_k, Gdk.KEY_K):
            self.start_calibration()
            return True
        if event.keyval in (Gdk.KEY_a, Gdk.KEY_A):
            self.apply()
            return True
        if event.keyval in (Gdk.KEY_r, Gdk.KEY_R):
            self.revert()
            return True
        return False


def revert_from_saved():
    """CLI rescue path: restore the matrix stored in latest.json."""
    path = os.path.join(DATA_DIR, "latest.json")
    try:
        with open(path) as f:
            data = json.load(f)
        device = data.get("device")
        previous = data.get("current_matrix")
    except (OSError, ValueError):
        print("no saved calibration in", path)
        return 1
    if not device or not previous:
        print("saved calibration has no previous matrix")
        return 1
    command = ["swaymsg", "input", device, "calibration_matrix"]
    command.extend("%.9f" % float(v) for v in previous)
    try:
        subprocess.run(command, env=sway_environ(), check=True)
    except Exception as exc:
        print("swaymsg failed:", exc)
        return 1
    try:
        os.unlink(SWAY_CALIBRATION_FILE)
    except OSError:
        pass
    print("restored", fmt_matrix(previous))
    return 0


def main():
    if "--revert" in sys.argv[1:]:
        return revert_from_saved()

    calibrate = "--calibrate" in sys.argv[1:]

    # Wayland app_id, same convention as the other touch apps.
    GLib.set_prgname("org.swaytablet.touchtest")
    try:
        Gdk.set_program_class("org.swaytablet.touchtest")
    except Exception:
        pass

    prov = Gtk.CssProvider()
    prov.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), prov,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    win = TouchTest()
    win.show_all()
    win.present()

    if calibrate:
        def deferred_calibration():
            if win.w > 1 and win.h > 1:
                win.start_calibration()
                return False
            return True

        GLib.timeout_add(100, deferred_calibration)

    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
