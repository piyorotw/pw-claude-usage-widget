"""Entry point: place one usage strip in the taskbar gap of each configured monitor.

    python main.py            run with a console (see errors while tuning)
    run.pyw / run.bat         run silently (for autostart)

Positioning is driven entirely by config.json so you can nudge each strip into the
empty part of your taskbar without touching code.
"""
from __future__ import annotations

import ctypes
import json
import sys
import threading
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

import usage
from bar import Bar, log

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"


def load_config() -> dict:
    # prefer the user's config.json; fall back to the shipped example (fresh clone)
    for path in (CONFIG_PATH, HERE / "config.example.json"):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    log("[config] no readable config.json or config.example.json")
    return {}


def _config_mtime():
    try:
        return CONFIG_PATH.stat().st_mtime
    except OSError:
        return None


def taskbar_height(screen) -> int:
    """Height of a bottom taskbar in logical px (0 if none / not at bottom)."""
    g = screen.geometry()
    a = screen.availableGeometry()
    h = g.bottom() - a.bottom()
    return h if h > 0 else 40


def real_taskbar_top(screen):
    """Actual top edge of the primary taskbar window in this screen's logical px.

    Qt's availableGeometry under-reports the taskbar height (the Shell_TrayWnd
    window extends higher than the reserved work area), so hugging just above it
    using availableGeometry leaves the strip overlapping — and thus covered. Read
    the real window rect instead. Returns None if unavailable.
    """
    if sys.platform != "win32":
        return None
    try:
        u = ctypes.windll.user32
        u.FindWindowW.restype = wintypes.HWND
        hwnd = u.FindWindowW("Shell_TrayWnd", None)
        if not hwnd:
            return None
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))          # physical px
        dpr = screen.devicePixelRatio() or 1.0
        return r.top / dpr                              # -> logical px
    except Exception:
        return None


def bar_geometry(screen, mon: dict, is_primary: bool = True):
    """Compute (x, y, w, h).

    Win11 will not let a normal window render *inside* the taskbar (topmost loses
    the z-order war; SetParent gets hidden behind the XAML taskbar). So by default
    the widget hugs the desktop just ABOVE the taskbar's top edge, where nothing
    competes for the topmost slot. Push it down with a positive `y_offset`.

    `scale` multiplies width+height (everything is drawn proportionally, so the
    whole widget grows/shrinks). The bottom-RIGHT corner stays anchored, so scaling
    grows the widget left+up and keeps it in the same corner. Tune it live in config.
    """
    g = screen.geometry()
    tb = taskbar_height(screen)
    scale = float(mon.get("scale", 1.0))
    base_w = int(mon.get("width", 360))
    base_h = int(mon.get("height", 110))
    w = max(1, int(base_w * scale))
    h = max(1, int(base_h * scale))
    # real_taskbar_top only knows the PRIMARY taskbar (Shell_TrayWnd); for other
    # monitors fall back to the work-area edge minus the usual ~24px the taskbar
    # window overhangs. Fine-tune per-monitor with y_offset.
    top = real_taskbar_top(screen) if is_primary else None
    if top is None:
        top = g.bottom() - tb - 24
    right = g.x() + int(mon.get("x", 200)) + base_w   # anchor: right edge of the base box
    x = right - w                                      # grow leftward as it scales up
    y = int(top) - h + int(mon.get("y_offset", 0))     # bottom stays on the taskbar edge
    return x, y, w, h


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        QGuiApplication.highDpiScaleFactorRoundingPolicy()
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    log("=== statusbar starting ===")

    config = load_config()
    screens = app.screens()
    primary = app.primaryScreen()
    monitors = config.get("monitors") or [{"screen_index": 0}]

    bars = []
    for mon in monitors:
        idx = int(mon.get("screen_index", 0))
        if idx >= len(screens):
            log(f"[monitor] screen_index {idx} not connected — skipped")
            continue
        geom = bar_geometry(screens[idx], mon, screens[idx] == primary)
        bar = Bar(geom, config)
        bar.apply_win_flags()   # set tool-window style before first show
        bar.show()
        bar.apply_win_flags()   # re-assert after the native window exists
        bars.append((bar, idx))

    if not bars:
        log("[fatal] no strips created — check config.monitors")
        return 1

    state = {"mtime": _config_mtime(), "config": config}

    # Manual refresh runs the (blocking) API call on a worker thread and hands the
    # result back to the GUI thread via this signal.
    class _Refresher(QObject):
        ready = Signal(object)

    refresher = _Refresher()

    def _apply(data):
        for bar, _ in bars:
            bar.update_data(data)

    refresher.ready.connect(_apply)

    def refresh():
        """Periodic, no-network: shows the last cached API result or the estimate."""
        try:
            data = usage.get_usage(state["config"], force=False)
            for bar, _ in bars:
                bar.update_data(data)
        except Exception as exc:
            log(f"[refresh] {exc!r}")

    def manual_refresh():
        """Logo click: fetch live from the API on a worker thread (no UI freeze)."""
        log("manual refresh (logo click)")
        for bar, _ in bars:
            bar.set_updating(True)

        def work():
            try:
                data = usage.get_usage(state["config"], force=True)
            except Exception as exc:
                log(f"[manual] {exc!r}")
                data = usage.get_usage(state["config"], force=False)
            refresher.ready.emit(data)

        threading.Thread(target=work, daemon=True).start()

    for bar, _ in bars:
        bar._on_logo = manual_refresh

    def tick():
        try:
            # cheap: re-read config only when the file actually changed (live tuning)
            mt = _config_mtime()
            if mt and mt != state["mtime"]:
                state["mtime"] = mt
                new_cfg = load_config()
                if new_cfg:
                    state["config"] = new_cfg
                    mons = new_cfg.get("monitors") or []
                    for (bar, idx), mon in zip(bars, mons):
                        if idx < len(screens):
                            bar.set_placement(
                                bar_geometry(screens[idx], mon, screens[idx] == primary),
                                new_cfg)
            for bar, _ in bars:
                bar.ensure_visible()   # self-heal if the shell hid/recreated us
                bar.reassert_topmost()
        except Exception as exc:
            log(f"[tick] {exc!r}")

    refresh()
    refresh_timer = QTimer()
    refresh_timer.timeout.connect(refresh)
    refresh_timer.start(int(config.get("refresh_seconds", 30)) * 1000)

    tick_timer = QTimer()
    tick_timer.timeout.connect(tick)
    tick_timer.start(1000)

    # keep timer refs alive
    app._timers = (refresh_timer, tick_timer)  # type: ignore[attr-defined]
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
