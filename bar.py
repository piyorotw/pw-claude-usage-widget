"""UI layer: one frameless, always-on-top strip drawn over the taskbar gap.

The widget is passive (a status display). On Windows it is kept topmost and, by
default, click-through so the taskbar underneath stays fully usable.
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QPointF, QRectF, QTimer
from PySide6.QtGui import (QColor, QFont, QPainter, QPainterPath, QPen, QPixmap,
                           QPolygonF)
from PySide6.QtWidgets import QWidget

HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / "statusbar.log"
LOGO_PATH = HERE / "CCLogo.png"


def log(msg: str):
    """File logger — pythonw has no console (sys.stderr is None)."""
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%H:%M:%S.%f} {msg}\n")
    except OSError:
        pass

# ---- palette (matches ExCCStatus.png reference) --------------------------
BG = QColor(32, 32, 36, 214)       # translucent dark-grey rounded card
TEXT = QColor(232, 232, 234)
MUTED = QColor(165, 165, 170)      # labels
RESET_TEXT = QColor(210, 210, 216)  # reset countdown — brighter so it reads clearly
TRACK = QColor(158, 158, 162)      # unfilled part of the meter (light grey)
FILL_TEXT = QColor(0, 0, 0)        # bold % centred on the coloured fill
PLACEHOLDER = QColor(70, 70, 72)   # "—" for rows with no data yet
GREEN = QColor(120, 226, 47)
AMBER = QColor(247, 224, 30)
RED = QColor(240, 45, 45)

# ---- Windows constants ---------------------------------------------------
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SW_SHOWNOACTIVATE = 4
GW_HWNDPREV = 3

# The z-order walk reads window HANDLES back from the API. Without restype=HWND,
# ctypes returns them as 32-bit ints and truncates/sign-flips them on 64-bit
# Windows, so the walk followed the wrong window and never saw the covering app.
# (Only the read helpers need this; SetWindowPos below works with plain ints.)
if sys.platform == "win32":
    _u32 = ctypes.windll.user32
    _u32.GetWindow.restype = wintypes.HWND
    _u32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _u32.GetWindowRect.restype = wintypes.BOOL
    _u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _u32.IsWindowVisible.restype = wintypes.BOOL
    _u32.IsWindowVisible.argtypes = [wintypes.HWND]


def _meter_color(pct: float) -> QColor:
    if pct >= 90:
        return RED
    if pct >= 70:
        return AMBER
    return GREEN


def _fmt_reset(epoch, now: float) -> str:
    if not epoch:
        return ""
    rem = int(epoch - now)
    if rem <= 0:
        return "now"
    d, rem = divmod(rem, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


class Bar(QWidget):
    def __init__(self, geometry, config: dict):
        super().__init__(None)
        self._cfg = config
        self._data = None
        # NB: deliberately NOT Qt.Tool — tool windows auto-hide when the app is
        # deactivated (e.g. clicking the taskbar Search box), which made the strip
        # vanish. WS_EX_TOOLWINDOW (set in apply_win_flags) keeps it off the
        # taskbar / Alt-Tab without that side effect.
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        x, y, w, h = geometry
        self.setGeometry(x, y, w, h)
        self._geometry = geometry
        self._logo = QPixmap(str(LOGO_PATH)) if LOGO_PATH.exists() else None
        self._logo_rect = None       # hit-box for the clickable logo (widget coords)
        self._on_logo = None         # callback set by main.py for a manual refresh
        self._updating = False

    # -- data ---------------------------------------------------------------
    def update_data(self, data: dict):
        self._data = data
        self._updating = False
        self.update()

    def set_updating(self, flag: bool):
        self._updating = flag
        self.update()

    # -- manual refresh on logo click --------------------------------------
    def mousePressEvent(self, ev):
        if (self._logo_rect and self._on_logo and not self._updating
                and self._logo_rect.contains(ev.position().toPoint())):
            self._on_logo()
        super().mousePressEvent(ev)

    def set_placement(self, geometry, config: dict):
        """Live-reposition/reconfigure (used by config hot-reload)."""
        self._cfg = config
        self._geometry = geometry
        self.setGeometry(*geometry)
        self.reassert_topmost()
        self.update()

    # -- self-healing ---------------------------------------------------------
    def hideEvent(self, ev):
        # The Win11 shell (Search/Start flyouts) can hide or even destroy
        # unowned topmost overlays. Whenever we get hidden from outside,
        # resurrect shortly after without stealing focus.
        log(f"hideEvent spontaneous={ev.spontaneous()}")
        super().hideEvent(ev)
        QTimer.singleShot(300, self.ensure_visible)

    def ensure_visible(self):
        """Bring the strip back if anything hid it or wiped its native styles."""
        if not self.isVisible():
            log("resurrect: show()")
            self.setGeometry(*self._geometry)
            self.show()
            self.apply_win_flags()
            return
        # Native window may have been recreated (fresh winId) with default
        # styles; detect the missing TOOLWINDOW bit and re-apply everything.
        if sys.platform == "win32":
            hwnd = int(self.winId())
            ex = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if not (ex & WS_EX_TOOLWINDOW) or not (ex & WS_EX_NOACTIVATE):
                log(f"styles lost (ex=0x{ex:X}) — reapplying")
                self.setGeometry(*self._geometry)
                self.apply_win_flags()

    # -- Windows styling ----------------------------------------------------
    def apply_win_flags(self):
        if sys.platform != "win32":
            return
        hwnd = int(self.winId())
        user32 = ctypes.windll.user32
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ex |= WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_LAYERED
        if self._cfg.get("click_through", True):
            ex |= WS_EX_TRANSPARENT
        else:
            ex &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
        # FRAMECHANGED makes the new ex-style (esp. TOOLWINDOW) take effect now.
        user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED | SWP_NOACTIVATE,
        )
        self._force_top()   # unconditionally rise above the taskbar on (re)start

    def _is_covered(self) -> bool:
        """True if ANY visible window sits above us in z-order and overlaps our
        rect — the taskbar, or a maximised app like Claude Code that stole the top
        spot even though we hold WS_EX_TOPMOST. We only re-top when this is true, so
        there is no needless z-order churn when nothing is on top of us."""
        user32 = ctypes.windll.user32
        me = int(self.winId())
        mine = wintypes.RECT()
        if not user32.GetWindowRect(me, ctypes.byref(mine)):
            return False
        h = user32.GetWindow(me, GW_HWNDPREV)   # first window ABOVE us in z-order
        depth = 0
        while h and depth < 500:
            if user32.IsWindowVisible(h):
                o = wintypes.RECT()
                user32.GetWindowRect(h, ctypes.byref(o))
                if o.left > -30000 and o.top > -30000:   # skip minimised sentinels
                    if not (o.right <= mine.left or o.left >= mine.right
                            or o.bottom <= mine.top or o.top >= mine.bottom):
                        return True
            h = user32.GetWindow(h, GW_HWNDPREV)
            depth += 1
        return False

    def _force_top(self):
        """Re-insert at the very top of the topmost band.

        A background window (WS_EX_NOACTIVATE) can't normally push itself above the
        *foreground* window's z-order, so a plain SetWindowPos(HWND_TOPMOST) silently
        does nothing when e.g. a maximised Claude Code is focused. Briefly attaching
        our thread's input to the foreground thread grants the permission; the
        NOTOPMOST->TOPMOST toggle then actually re-orders us to the top.
        """
        user32 = ctypes.windll.user32
        hwnd = int(self.winId())
        fg = user32.GetForegroundWindow()
        my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        if fg_tid and fg_tid != my_tid:
            attached = bool(user32.AttachThreadInput(my_tid, fg_tid, True))
        try:
            f = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, f)
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, f | SWP_SHOWWINDOW)
            user32.BringWindowToTop(hwnd)
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        finally:
            if attached:
                user32.AttachThreadInput(my_tid, fg_tid, False)

    def reassert_topmost(self):
        """Keep the widget on top. Re-orders only when something actually covers it
        (taskbar, Claude Code, any window) — no needless z-order churn otherwise.
        Coordinates are never touched, so Qt keeps its own DPI-correct placement."""
        if sys.platform != "win32":
            return
        try:
            if self._is_covered():
                self._force_top()
        except Exception as exc:  # never let a bad handle kill the timer
            log(f"reassert error: {exc!r}")

    # -- painting -----------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = self.rect().adjusted(0, 0, -1, -1)
        H = r.height()

        radius = max(10, int(H * 0.16))
        path = QPainterPath()
        path.addRoundedRect(QRectF(r), radius, radius)
        p.fillPath(path, BG)

        d = (self._data or {}).get("claude")
        now = (self._data or {}).get("now", time.time())
        rows = (d or {}).get("rows") or []
        if not rows:
            msg = "loading…" if not self._data else (d.get("error") if d else "no data")
            self._text(p, QRectF(r.left() + 12, r.top(), r.width() - 24, H),
                       msg or "no data", MUTED, max(9, int(H * 0.16)))
            p.end()
            return

        pad = max(6, int(H * 0.09))
        # logo (rounded square, smaller than full height, vertically centred)
        rows_x = r.left() + pad
        if self._logo and not self._logo.isNull():
            sz = int(H * 0.70)                       # smaller than the card height
            lm = int(H * 0.10)                       # left margin
            lr = QRectF(r.left() + lm, r.top() + (H - sz) / 2, sz, sz)
            self._logo_rect = lr.toRect()
            clip = QPainterPath()
            clip.addRoundedRect(lr, sz * 0.28, sz * 0.28)
            p.save()
            p.setClipPath(clip)
            p.drawPixmap(lr.toRect(), self._logo)
            if self._updating:      # dim the logo while a manual refresh runs
                p.fillRect(lr, QColor(0, 0, 0, 120))
            p.restore()
            rows_x = int(lr.right() + H * 0.11)

        rows_w = r.right() - pad - rows_x
        n = len(rows)
        pad_v = int(H * 0.09)
        row_h = (H - 2 * pad_v) / n
        for i, row in enumerate(rows):
            ry = r.top() + pad_v + i * row_h
            self._draw_row(p, rows_x, ry, rows_w, row_h, row, now)
        p.end()

    def _draw_row(self, p, x, y, w, h, row, now):
        placeholder = row.get("placeholder") or row.get("pct") is None
        pct = row.get("pct") or 0.0
        est = row.get("est")
        reset = _fmt_reset(row.get("reset"), now) or "—"    # always show something

        bar_h = max(6, int(h * 0.72))           # thick pill like the reference
        bar_y = y + (h - bar_h) / 2
        # ONE font size for label / % / reset so they all match; low floor so the
        # widget really shrinks when you lower "scale".
        fpx = max(7, int(bar_h * 0.60))
        icon_r = fpx * 0.60                       # refresh icon sized to the text

        label_w = max(14, int(w * 0.10))
        reset_w = max(40, int(w * 0.42))
        gap = max(3, int(w * 0.02))
        bar_x = x + label_w + gap
        bar_w = w - label_w - gap - reset_w - gap

        # label ("5h" / "Wk" / "Fb")
        self._text(p, QRectF(x, y, label_w, h), row.get("label", ""), MUTED, fpx,
                   bold=True, align=Qt.AlignRight | Qt.AlignVCenter)

        # meter: light-grey track + coloured fill
        p.setPen(Qt.NoPen)
        p.setBrush(TRACK)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), bar_h / 2, bar_h / 2)
        if not placeholder:
            fw = max(0.0, min(1.0, pct / 100.0)) * bar_w
            if fw > 0:
                p.setBrush(_meter_color(pct))
                p.drawRoundedRect(QRectF(bar_x, bar_y, max(fw, bar_h), bar_h),
                                  bar_h / 2, bar_h / 2)
        # centred value (bold black %, or a grey "—" placeholder)
        if placeholder:
            txt, col = "—", PLACEHOLDER
        else:
            txt, col = (f"~{pct:.0f}%" if est else f"{pct:.0f}%"), FILL_TEXT
        self._text(p, QRectF(bar_x, bar_y, bar_w, bar_h), txt, col, fpx,
                   bold=True, align=Qt.AlignCenter)

        # reset: refresh icon + countdown — same font size as the labels
        rx = bar_x + bar_w + gap
        self._draw_reset_icon(p, rx + icon_r, y + h / 2, icon_r)
        self._text(p, QRectF(rx + icon_r * 2 + max(4, int(fpx * 0.4)), y,
                             reset_w - icon_r * 2 - max(4, int(fpx * 0.4)), h),
                   reset, RESET_TEXT, fpx, bold=True,
                   align=Qt.AlignLeft | Qt.AlignVCenter)

    def _draw_reset_icon(self, p, cx, cy, r):
        """A bold clockwise circular-refresh arrow (matches the reference glyph)."""
        import math
        pen = QPen(MUTED, max(1.8, r * 0.36))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        # arc leaving a gap at the top-right (Qt angles: 0°=3o'clock, CCW positive)
        start, span = 95, 250
        p.drawArc(rect, int(start * 16), int(span * 16))
        # arrowhead at the arc's start end (top-right), pointing clockwise
        ang = math.radians(start)
        ex = cx + r * math.cos(ang)
        ey = cy - r * math.sin(ang)
        tx, ty = math.sin(ang), math.cos(ang)     # clockwise tangent at that point
        a = r * 1.0
        tip = QPointF(ex + tx * a, ey + ty * a)
        back = QPointF(ex - tx * a * 0.15, ey - ty * a * 0.15)
        perp = QPointF(-ty, tx)
        b1 = QPointF(back.x() + perp.x() * a * 0.7, back.y() + perp.y() * a * 0.7)
        b2 = QPointF(back.x() - perp.x() * a * 0.7, back.y() - perp.y() * a * 0.7)
        p.setPen(Qt.NoPen)
        p.setBrush(MUTED)
        p.drawPolygon(QPolygonF([tip, b1, b2]))

    def _text(self, p, rect, text, color, px, bold=False,
              align=Qt.AlignLeft | Qt.AlignVCenter):
        f = QFont("Segoe UI")
        f.setPixelSize(int(px))
        f.setBold(bold)
        p.setFont(f)
        p.setPen(color)
        p.drawText(rect, int(align), text)
