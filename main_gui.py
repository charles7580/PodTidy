"""
PodTidy — Podcast Audio Organizer
==================================
Windows desktop GUI for batch-organising podcast audio files.
Drag in audio files, and the app auto-detects the podcast, formats
ID3 tags, normalises ReplayGain, and distributes files into the
correct podcast folder.
"""

# ---------------------------------------------------------------------------
# 1. Standard library
# ---------------------------------------------------------------------------
import os
import sys
import json
import re
import threading
from ctypes import windll

# ---------------------------------------------------------------------------
# 2. High-DPI awareness — MUST come before any GUI library import
# ---------------------------------------------------------------------------
windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor DPI V2

# ---------------------------------------------------------------------------
# 3. GUI libraries
# ---------------------------------------------------------------------------
import tkinter as tk
import customtkinter as ctk
from tkinter import filedialog

try:
    from tkinterdnd2 import TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

# ---------------------------------------------------------------------------
# 4. Business logic
# ---------------------------------------------------------------------------
from podcast_engine import (
    PodcastEngine,
    SUPPORTED_EXTS,
    PODCAST_NAMES,
)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _get_base_dir():
    """PyInstaller-safe base directory."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _get_config_dir():
    """Writable directory for config (exe-adjacent when frozen)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _get_base_dir()
CONFIG_DIR = _get_config_dir()
CONFIG_PATH = os.path.join(CONFIG_DIR, ".app_config.json")


# ---------------------------------------------------------------------------
# Configuration persistence
# ---------------------------------------------------------------------------

def load_config():
    defaults = {
        "last_podcast_root": "",
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            defaults.update(data)
        except Exception:
            pass
    return defaults


def save_config(config):
    safe = {k: v for k, v in config.items() if k in ("last_podcast_root",)}
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(safe, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Windows theme detection
# ---------------------------------------------------------------------------

def _get_windows_theme():
    """Read HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize\\AppsUseLightTheme"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return "dark" if value == 0 else "light"
    except Exception:
        return "dark"


def _get_windows_accent():
    """Read HKCU\\...\\DWM\\AccentColor (ABGR → RGB)"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\DWM",
        )
        value, _ = winreg.QueryValueEx(key, "AccentColor")
        winreg.CloseKey(key)
        # value is ABGR, extract RGB
        b = (value >> 24) & 0xFF
        g = (value >> 16) & 0xFF
        r = (value >> 8) & 0xFF
        return f"#{r:02X}{g:02X}{b:02X}"
    except Exception:
        return "#6B69D6"


def _blend_hex(c1, c2, t):
    """Linearly blend two hex colours.  t=0 → c1,  t=1 → c2."""
    def _rgb(h): return (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16))
    r1, g1, b1 = _rgb(c1)
    r2, g2, b2 = _rgb(c2)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02X}{g:02X}{b:02X}"


def _build_colors():
    theme = _get_windows_theme()
    accent = _get_windows_accent()

    if theme == "dark":
        return {
            "bg_main": "#1C1C1E",
            "card_surface": "#2C2C2E",
            "accent_purple": accent,
            "accent_hover": _blend_hex(accent, "#FFFFFF", 0.15),
            "text_primary": "#FFFFFF",
            "text_secondary": "#AEAEB2",
            "text_dim": "#6E6E73",
            "border_light": "#48484A",
            "border_dash": "#5A5A5E",
            "error": "#FF453A",
            "success": "#30D158",
            "white": "#FFFFFF",
            "card_hover": "#3A3A3C",
            "shadow_outer": "#000000",
        }
    else:
        return {
            "bg_main": "#FAF8F5",
            "card_surface": "#FFFFFF",
            "accent_purple": accent,
            "accent_hover": _blend_hex(accent, "#000000", 0.85),
            "text_primary": "#1E1E24",
            "text_secondary": "#8E8E93",
            "text_dim": "#AEAEB2",
            "border_light": "#D1D1D6",
            "border_dash": "#C6C6C8",
            "error": "#FF453A",
            "success": "#34C759",
            "white": "#FFFFFF",
            "card_hover": "#F2F2F7",
            "shadow_outer": "#000000",
        }


COLORS = _build_colors()
FONT_FAMILY = "Microsoft YaHei"

FONTS = {
    "heading": (FONT_FAMILY, 18, "bold"),
    "label": (FONT_FAMILY, 13),
    "button": (FONT_FAMILY, 14, "bold"),
    "hint": (FONT_FAMILY, 13),
    "file_count": (FONT_FAMILY, 13, "bold"),
    "status": (FONT_FAMILY, 12),
}


# ---------------------------------------------------------------------------
# Canvas drop-zone (MainZone)
# ---------------------------------------------------------------------------

class MainZone(tk.Canvas):
    """Drag-and-drop card with three visual states: empty / files-selected / processing."""

    _SPINNER_R = 20           # <-- 圆圈半径 (px)
    _SPINNER_WIDTH = 5        # <-- 弧线粗细 (px)
    _SPINNER_SPEED = 40       # <-- 帧间隔 (ms), 越小越快

    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            bg=COLORS["bg_main"],
            highlightthickness=0,
            bd=0,
            **kwargs,
        )
        self._file_count = 0
        self._processing = False
        self._message = ""
        self._percent = 0
        self._dragover = False
        self._spin_angle = 0
        self._spin_after_id = None
        self._file_paths = []

        self.bind("<Configure>", self._on_resize)
        self.after(100, self._draw)

    # ---- Public API ----

    def set_files(self, paths: list[str]) -> None:
        self._file_paths = list(dict.fromkeys(paths))  # dedupe, keep order
        self._file_count = len(self._file_paths)
        self._draw()

    def get_files(self) -> list[str]:
        return list(self._file_paths)

    def clear_files(self) -> None:
        self._file_paths = []
        self._file_count = 0
        self._draw()

    def set_processing(self, active: bool, message: str = "",
                       percent: int = 0) -> None:
        self._processing = active
        self._message = message
        self._percent = percent
        if active:
            self._start_spin()
        else:
            self._stop_spin()
        self._draw()

    def set_dragover(self, active: bool) -> None:
        self._dragover = active
        self._draw()

    # ---- Drawing ----

    def _on_resize(self, event):
        if event.width > 10:
            self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 20 or h < 20:
            return

        r = 24  # corner radius
        cx, cy = w // 2, h // 2

        # --- Card background ---
        self._round_rect(4, 4, w - 4, h - 4, r,
                         fill=COLORS["card_surface"], outline="")

        # --- Dashed border ---
        dash_color = (COLORS["accent_purple"]
                      if self._dragover
                      else COLORS["border_dash"])
        dash_width = 3 if self._dragover else 2
        self._round_border(4, 4, w - 4, h - 4, r,
                           color=dash_color, width=dash_width, dash=(8, 6))

        # --- Content ---
        if self._processing:
            self._draw_processing(cx, cy, w, h)
        elif self._file_count > 0:
            self._draw_selected(cx, cy)
        else:
            self._draw_empty(cx, cy)

    def _draw_empty(self, cx, cy):
        self.create_text(cx, cy - 8,
                         text="将音频文件拖到此处",
                         font=(FONT_FAMILY, 14, "bold"),
                         fill=COLORS["text_primary"], anchor="center")
        self.create_text(cx, cy + 44,
                         text="或点击浏览文件",
                         font=(FONT_FAMILY, 11),
                         fill=COLORS["text_dim"], anchor="center")

    def _draw_selected(self, cx, cy):
        self.create_text(cx, cy - 41,
                         text=f"已选择 {self._file_count} 个音频文件",
                         font=(FONT_FAMILY, 13, "bold"),
                         fill=COLORS["text_primary"], anchor="center")
        self.create_text(cx, cy + 3,
                         text="点击下方按钮开始整理",
                         font=(FONT_FAMILY, 11),
                         fill=COLORS["text_secondary"], anchor="center")
        self.create_text(cx, cy + 41,
                         text="或继续拖放添加更多文件",
                         font=(FONT_FAMILY, 11),
                         fill=COLORS["text_dim"], anchor="center")

    def _draw_processing(self, cx, cy, w, h):
        accent = COLORS["accent_purple"]
        spinner_y = cy - 72         # <-- 圆圈垂直位置: cy 为卡片中心, 负值=往上

        # Spinning arc
        r = self._SPINNER_R
        self.create_arc(
            cx - r, spinner_y - r, cx + r, spinner_y + r,
            start=self._spin_angle, extent=270,
            style="arc", outline=accent, width=self._SPINNER_WIDTH,
        )

        # Status message — "正在处理第 X/N 个文件..."
        self.create_text(cx, cy - 20, text=self._message,   # <-- 主消息 Y 偏移
                         font=(FONT_FAMILY, 13),
                         fill=COLORS["text_primary"], anchor="center")

        # Sub-line
        self.create_text(cx, cy + 22,                         # <-- 副文字 Y 偏移
                         text="处理中, 请稍候...",
                         font=(FONT_FAMILY, 11),
                         fill=COLORS["text_dim"], anchor="center")

        self.create_text(cx, h - 28,                          # <-- 底部取消提示距底边距离
                         text="关闭窗口可取消处理",
                         font=(FONT_FAMILY, 11),
                         fill=COLORS["text_dim"], anchor="center")

    # ---- Spinner animation ----

    def _start_spin(self):
        if self._spin_after_id is not None:
            return

        def _tick():
            self._spin_angle = (self._spin_angle - 12) % 360
            self._spin_after_id = None
            if self._processing:
                self._draw()
                self._spin_after_id = self.after(self._SPINNER_SPEED, _tick)

        self._spin_after_id = self.after(self._SPINNER_SPEED, _tick)

    def _stop_spin(self):
        if self._spin_after_id is not None:
            self.after_cancel(self._spin_after_id)
            self._spin_after_id = None
        self._spin_angle = 0

    # ---- Rounded-rect helpers ----

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        self.create_rectangle(x1 + r, y1, x2 - r, y2, **kw)
        self.create_rectangle(x1, y1 + r, x2, y2 - r, **kw)
        self.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r,
                        start=90, extent=90, style="pieslice", **kw)
        self.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r,
                        start=0, extent=90, style="pieslice", **kw)
        self.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2,
                        start=180, extent=90, style="pieslice", **kw)
        self.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2,
                        start=270, extent=90, style="pieslice", **kw)

    def _round_border(self, x1, y1, x2, y2, r, color, width, dash):
        # Create arcs + lines for the dashed border
        segments = [
            # top edge
            ("line", x1 + r, y1, x2 - r, y1),
            # right edge
            ("line", x2, y1 + r, x2, y2 - r),
            # bottom edge
            ("line", x2 - r, y2, x1 + r, y2),
            # left edge
            ("line", x1, y2 - r, x1, y1 + r),
            # top-left corner
            ("arc", x1, y1, x1 + 2 * r, y1 + 2 * r, 90, 90),
            # top-right corner
            ("arc", x2 - 2 * r, y1, x2, y1 + 2 * r, 0, 90),
            # bottom-right corner
            ("arc", x2 - 2 * r, y2 - 2 * r, x2, y2, 270, 90),
            # bottom-left corner
            ("arc", x1, y2 - 2 * r, x1 + 2 * r, y2, 180, 90),
        ]
        for seg in segments:
            if seg[0] == "line":
                _, sx, sy, ex, ey = seg
                self.create_line(sx, sy, ex, ey,
                                 fill=color, width=width,
                                 dash=dash)
            else:
                _, ax, ay, ax2, ay2, start, extent = seg
                self.create_arc(ax, ay, ax2, ay2,
                                start=start, extent=extent,
                                style="arc", outline=color,
                                width=width, dash=dash)


# ---------------------------------------------------------------------------
# Canvas textured button
# ---------------------------------------------------------------------------

class _TexturedButton(tk.Canvas):
    """Metal-textured button with gradient fill, shadow, and press feedback."""

    def __init__(self, master, text: str, command=None,
                 height=84, **kwargs):
        super().__init__(master, height=height,
                         bg=COLORS["bg_main"], highlightthickness=0,
                         bd=0, **kwargs)
        self._text = text
        self._command = command
        self._h = height
        self._hover = False
        self._pressed = False
        self._enabled = True

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", self._on_resize)
        self.after(100, self._draw)

    def _on_enter(self, e):
        if self._enabled:
            self._hover = True
            self._draw()

    def _on_leave(self, e):
        self._hover = False
        self._pressed = False
        self._draw()

    def _on_press(self, e):
        if self._enabled:
            self._pressed = True
            self._draw()

    def _on_release(self, e):
        if self._enabled and self._pressed:
            self._pressed = False
            self._draw()
            if self._command:
                self._command()

    def _on_resize(self, e):
        if e.width > 10:
            self._draw()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self._hover = False
        self._pressed = False
        self._draw()

    def set_text(self, text: str):
        self._text = text
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self._h
        r = 24
        if w < 20:
            return

        accent = COLORS["accent_purple"]
        is_dark = COLORS["bg_main"] != "#FAF8F5"

        # 1. Bottom shadows (when not pressed & enabled)
        if not self._pressed and self._enabled:
            for off, alpha in [(4, 0.08), (2, 0.05)]:
                sc = _blend_hex(COLORS["shadow_outer"],
                                COLORS["bg_main"], 1 - alpha)
                self._round_rect(off, off, w - off, h + off, r,
                                 fill=sc, outline="")

        # 2. Main gradient
        if is_dark:
            stops = [
                (0, accent),
                (0.5, _blend_hex(accent, "#FFFFFF", 0.2)),
                (1, _blend_hex(accent, "#000000", 0.35)),
            ]
        else:
            stops = [
                (0, "#EEEEEE"),
                (0.4, "#F5F5F5"),
                (0.7, "#D8D8D8"),
                (1, "#B8B8B8"),
            ]

        if self._pressed:
            stops = [(t, _blend_hex(c, "#000000", 0.12)) for t, c in stops]

        for y in range(h):
            t = y / h
            color = self._gradient(t, stops)
            indent = 0
            if y < r:
                dy = r - y
                indent = r - int((r * r - dy * dy) ** 0.5)
            elif y > h - r:
                dy = y - (h - r)
                indent = r - int((r * r - dy * dy) ** 0.5)
            if indent < w // 2:
                self.create_line(indent, y, w - indent, y,
                                 fill=color, width=1)

        # 3. Top highlight line (two pixels)
        hl = _blend_hex("#FFFFFF", stops[0][1], 0.7)
        self.create_line(r, 0, w - r, 0, fill=hl, width=1)
        self.create_line(r, 1, w - r, 1,
                         fill=_blend_hex(hl, stops[0][1], 0.5), width=1)

        # 4. Pressed inner shadow
        if self._pressed:
            self._round_rect(2, 2, w - 2, h - 2, r - 2,
                             fill="",
                             outline=_blend_hex(accent, "#000000", 0.4),
                             width=1)
            for i in range(6):
                dark = _blend_hex("#000000", accent, i / 10)
                self.create_line(r, i, w - r, i, fill=dark, width=1)

        # 5. Text with shadow
        if is_dark:
            txt_c = COLORS["white"]
            shadow_c = _blend_hex("#000000", accent, 0.7)
        else:
            txt_c = COLORS["text_primary"]
            shadow_c = "#FFFFFF"

        y_off = 2 if self._pressed else 0
        font_sz = 14
        self.create_text(w // 2, h // 2 + y_off + 1, text=self._text,
                         font=(FONT_FAMILY, font_sz, "bold"),
                         fill=shadow_c, anchor="center")
        self.create_text(w // 2, h // 2 + y_off, text=self._text,
                         font=(FONT_FAMILY, font_sz, "bold"),
                         fill=txt_c, anchor="center")

        # 6. Disabled overlay
        if not self._enabled:
            self.create_rectangle(0, 0, w, h,
                                  fill=_blend_hex(COLORS["bg_main"],
                                                  "#000000", 0.3),
                                  outline="", stipple="gray50")

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        self.create_rectangle(x1 + r, y1, x2 - r, y2, **kw)
        self.create_rectangle(x1, y1 + r, x2, y2 - r, **kw)
        self.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r,
                        start=90, extent=90, style="pieslice", **kw)
        self.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r,
                        start=0, extent=90, style="pieslice", **kw)
        self.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2,
                        start=180, extent=90, style="pieslice", **kw)
        self.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2,
                        start=270, extent=90, style="pieslice", **kw)

    def _gradient(self, t, stops):
        for i in range(len(stops) - 1):
            t0, c0 = stops[i]
            t1, c1 = stops[i + 1]
            if t0 <= t <= t1:
                return _blend_hex(c0, c1, (t - t0) / (t1 - t0))
        return stops[-1][1]


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class PodTidyApp(ctk.CTk if not DND_AVAILABLE else TkinterDnD.Tk):
    """PodTidy main window."""

    def __init__(self):
        super().__init__()
        self.withdraw()

        # --- Config ---
        self._config = load_config()
        self._podcast_root = self._config.get("last_podcast_root", "")
        self._is_processing = False
        self._engine = None

        # --- Window setup ---
        self.title("PodTidy - 播客整理工具 1.5")
        self._apply_window_icon()
        self._apply_titlebar_theme()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # --- Build UI ---
        self._build_ui()
        self._center_window()
        self.deiconify()

    # ==================================================================
    # Title-bar theme
    # ==================================================================

    def _apply_window_icon(self):
        """Set the title-bar / taskbar icon from app_icon.ico."""
        # Search order: next to exe/script, then in _MEIPASS (PyInstaller)
        candidates = [
            os.path.join(CONFIG_DIR, "app_icon.ico"),
            os.path.join(BASE_DIR, "app_icon.ico"),
        ]
        for ico in candidates:
            if os.path.isfile(ico):
                try:
                    # Use tk.call for maximum compatibility on Windows
                    self.tk.call("wm", "iconbitmap", self._w, "-default", ico)
                    return
                except Exception:
                    try:
                        self.iconbitmap(default=ico)
                        return
                    except Exception:
                        pass

    def _apply_titlebar_theme(self):
        def _set():
            try:
                hwnd = windll.user32.GetAncestor(
                    self.winfo_id(), 2)  # GA_ROOT=2
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                is_dark = 1 if _get_windows_theme() == "dark" else 0
                windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                    windll.byref(windll.c_int(is_dark)),
                    windll.sizeof(windll.c_int(is_dark)),
                )
            except Exception:
                pass
        self.after(100, _set)

    # ==================================================================
    # Layout
    # ==================================================================

    def _build_ui(self):
        PAD = 48

        # Row weights: only the drop-zone expands
        self.grid_rowconfigure(0, weight=5)   # drop zone (elastic, TOP)
        self.grid_rowconfigure(1, weight=0)   # settings bar (centered, MIDDLE)
        self.grid_rowconfigure(2, weight=0)   # progress bar (hidden by default)
        self.grid_rowconfigure(3, weight=0)   # action button (fixed, BOTTOM)
        self.grid_columnconfigure(0, weight=1)

        self.configure(bg=COLORS["bg_main"])

        # ---- Row 0: Drop zone (TOP) ----
        self._main_zone = MainZone(self)
        self._main_zone.grid(row=0, column=0, sticky="nsew",
                             padx=PAD, pady=(PAD, 0))

        # ---- Row 1: Settings bar (MIDDLE, centered) ----
        self._build_settings_row(PAD)

        # ---- Row 2: Progress bar (hidden) ----
        self._progress_bar = ctk.CTkProgressBar(
            self, fg_color=COLORS["card_surface"],
            progress_color=COLORS["accent_purple"],
            height=6, corner_radius=3,
        )
        self._progress_bar.set(0)

        # ---- Row 3: Action button (BOTTOM) ----
        self._btn_action = _TexturedButton(
            self, text="开始整理",
            command=self._on_action_clicked,
            height=84, width=400,
        )
        self._btn_action.grid(row=3, column=0, pady=(32, PAD))

        # ---- DND registration ----
        if DND_AVAILABLE:
            try:
                self.drop_target_register("DND_Files")
                self.dnd_bind("<<DragEnter>>", self._on_drag_enter)
                self.dnd_bind("<<DragLeave>>", self._on_drag_leave)
                self.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

        # ---- Click-to-browse on the drop zone ----
        self._main_zone.bind("<ButtonPress-1>", self._on_zone_press)
        self._main_zone.bind("<ButtonRelease-1>", self._on_zone_release)
        self._click_start = (0, 0)

    def _build_settings_row(self, PAD):
        """Podcast root directory selector — centered between drop zone and button."""
        # Outer frame stretches full width so we can center the inner frame
        outer = ctk.CTkFrame(self, fg_color="transparent")
        outer.grid(row=1, column=0, sticky="ew", padx=PAD, pady=(16, 0))
        outer.grid_columnconfigure(0, weight=1)  # <-- push inner to center

        inner = ctk.CTkFrame(outer, fg_color="transparent")
        inner.grid(row=0, column=0)  # <-- centered by weight=1 on column 0

        fmt_font = ctk.CTkFont(family=FONT_FAMILY, size=13)

        dir_btn = ctk.CTkButton(
            inner, text="播客目录...",
            font=fmt_font,
            fg_color=COLORS["card_surface"],
            hover_color=COLORS["card_hover"],
            text_color=COLORS["text_primary"],
            border_color=COLORS["border_light"],
            border_width=1,
            corner_radius=8,
            command=self._on_select_podcast_root,
        )
        dir_btn.pack(side="left", padx=(0, 8))

        display = (
            os.path.basename(self._podcast_root)
            if self._podcast_root
            else "未设置"
        )
        self._root_label = ctk.CTkLabel(
            inner,
            text=display,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLORS["text_dim"],
        )
        self._root_label.pack(side="left")

        # Matched podcasts indicator (second line, below the directory selector)
        self._match_label = ctk.CTkLabel(
            outer,
            text="",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLORS["text_dim"],
        )
        self._match_label.grid(row=1, column=0, pady=(6, 0))

        # Initial scan if directory is already set (from config)
        if self._podcast_root:
            self.after(50, self._refresh_podcast_match)

    # ==================================================================
    # Callbacks — directory selection
    # ==================================================================

    def _on_select_podcast_root(self):
        directory = filedialog.askdirectory(
            title="选择播客根目录",
            initialdir=(
                self._podcast_root
                if self._podcast_root
                else os.path.expanduser("~")
            ),
        )
        if directory:
            self._podcast_root = directory
            self._config["last_podcast_root"] = directory
            save_config(self._config)
            self._root_label.configure(text=os.path.basename(directory))
            self._refresh_podcast_match()

    # ==================================================================
    # Podcast directory matching
    # ==================================================================

    def _scan_podcast_dirs(self, root_dir: str) -> list[str]:
        """Return podcast names whose subdirectory exists and contains album art."""
        matched = []
        if not root_dir or not os.path.isdir(root_dir):
            return matched
        for name in PODCAST_NAMES:
            subdir = os.path.join(root_dir, name)
            if not os.path.isdir(subdir):
                continue
            for art in ("folder.jpg", "folder.jpeg", "folder.png"):
                if os.path.isfile(os.path.join(subdir, art)):
                    matched.append(name)
                    break
        return matched

    def _refresh_podcast_match(self):
        """Scan the current podcast root and update the match indicator label."""
        if not self._podcast_root or not os.path.isdir(self._podcast_root):
            self._match_label.configure(text="", text_color=COLORS["text_dim"])
            return

        matched = self._scan_podcast_dirs(self._podcast_root)
        if matched:
            names = "、".join(matched)
            self._match_label.configure(
                text=f"已匹配播客: {names}",
                text_color=COLORS["text_dim"],
            )
        else:
            self._match_label.configure(
                text="⚠ 未检测到匹配的播客文件夹（需包含 folder.jpg/png）",
                text_color=COLORS["error"],
            )

    # ==================================================================
    # Callbacks — drag & drop
    # ==================================================================

    def _on_drag_enter(self, event):
        self._main_zone.set_dragover(True)

    def _on_drag_leave(self, event):
        self._main_zone.set_dragover(False)

    def _on_drop(self, event):
        self._main_zone.set_dragover(False)
        raw = getattr(event, "data", "")
        files = self._parse_drop_data(raw)
        if files:
            expanded = self._expand_files(files)
            if expanded:
                self._main_zone.set_files(expanded)
            else:
                self._show_error("没有识别到支持的音频文件（.mp3, .m4a, .flac, .wav 等）")

    def _parse_drop_data(self, raw: str) -> list[str]:
        """Parse Windows DND data: {path1} {path2}"""
        files = []
        pattern = re.compile(r"\{(.+?)\}")
        matches = pattern.findall(raw)
        if matches:
            for m in matches:
                path = m.strip()
                if os.path.isfile(path) or os.path.isdir(path):
                    files.append(path)
        else:
            for part in raw.split():
                part = part.strip().strip("{}")
                if os.path.isfile(part) or os.path.isdir(part):
                    files.append(part)
        return files

    # ==================================================================
    # Callbacks — click to browse
    # ==================================================================

    def _on_zone_press(self, event):
        self._click_start = (event.x, event.y)

    def _on_zone_release(self, event):
        dx = abs(event.x - self._click_start[0])
        dy = abs(event.y - self._click_start[1])
        if dx < 5 and dy < 5:
            self._on_browse_files()

    def _on_browse_files(self):
        if self._is_processing:
            return
        filetypes = [
            ("音频文件", "*.mp3;*.m4a;*.flac;*.wma;*.aac;*.ogg;*.wav"),
            ("所有文件", "*.*"),
        ]
        paths = filedialog.askopenfilenames(
            title="选择音频文件",
            filetypes=filetypes,
        )
        if paths:
            expanded = self._expand_files(list(paths))
            if expanded:
                self._main_zone.set_files(expanded)
            else:
                self._show_error("没有识别到支持的音频文件")

    # ==================================================================
    # File helpers
    # ==================================================================

    def _expand_files(self, paths: list[str]) -> list[str]:
        """Recursively expand directories, filter to supported extensions."""
        all_files = []
        for path in paths:
            if os.path.isdir(path):
                for root, _dirs, filenames in os.walk(path):
                    for fn in filenames:
                        ext = os.path.splitext(fn)[1].lower()
                        if ext in SUPPORTED_EXTS:
                            all_files.append(os.path.join(root, fn))
            elif os.path.isfile(path):
                ext = os.path.splitext(path)[1].lower()
                if ext in SUPPORTED_EXTS:
                    all_files.append(path)
        return list(dict.fromkeys(all_files))  # dedupe, keep order

    # ==================================================================
    # Callbacks — action button
    # ==================================================================

    def _on_action_clicked(self):
        if self._is_processing:
            return

        files = self._main_zone.get_files()
        if not files:
            self._show_error("请先拖入或选择音频文件")
            return

        if not self._podcast_root or not os.path.isdir(self._podcast_root):
            self._show_error("请先选择一个有效的播客根目录")
            return

        self._start_processing(files)

    # ==================================================================
    # Processing lifecycle
    # ==================================================================

    def _start_processing(self, files: list[str]):
        self._is_processing = True
        self._btn_action.set_enabled(False)
        self._btn_action.set_text("整理中...")
        self._main_zone.set_processing(True, "正在启动...")
        self._progress_bar.set(0)
        self._progress_bar.grid(
            row=2, column=0, sticky="ew",
            padx=48, pady=(8, 0),
        )

        self._engine = PodcastEngine(
            progress_callback=self._on_progress,
            log_callback=self._on_log,
            complete_callback=self._on_complete,
        )
        self._engine.start(files, self._podcast_root)

    def _on_progress(self, message: str, percent: int = -1,
                     is_error: bool = False):
        """Called from worker thread — marshal to main thread."""
        self.after(0, lambda: (
            self._main_zone.set_processing(True, message, percent),
            self._progress_bar.set(max(0, percent) / 100.0),
        ))

    def _on_log(self, message: str):
        """Called from worker thread — marshal to main thread."""
        self.after(0, lambda: print(message, flush=True))

    def _on_complete(self, success: bool, message: str,
                     results: list[dict]):
        """Called from worker thread — marshal to main thread."""
        self.after(0, lambda: self._handle_completion(success, message, results))

    def _handle_completion(self, success: bool, message: str,
                           results: list[dict]):
        self._is_processing = False
        self._main_zone.set_processing(False)
        self._progress_bar.grid_remove()
        self._btn_action.set_enabled(True)
        self._btn_action.set_text("开始整理")

        # Clear the file list
        self._main_zone.clear_files()

        # Show completion dialog
        self._show_completion_dialog(success, message, results)

    # ==================================================================
    # Dialogs
    # ==================================================================

    def _show_error(self, msg: str):
        _DLG_W, _DLG_H = 360, 160
        dlg = ctk.CTkToplevel(self)
        dlg.title("PodTidy")
        dlg.geometry(f"{_DLG_W}x{_DLG_H}")
        dlg.resizable(False, False)
        dlg.configure(fg_color=COLORS["bg_main"])
        dlg.transient(self)
        dlg.grab_set()
        self._center_dialog(dlg, _DLG_W, _DLG_H)

        ctk.CTkLabel(
            dlg, text=msg,
            font=ctk.CTkFont(family=FONT_FAMILY, size=13),
            text_color=COLORS["text_primary"],
            wraplength=_DLG_W - 48, justify="left",
        ).pack(pady=(32, 20))

        ctk.CTkButton(
            dlg, text="确定",
            fg_color=COLORS["accent_purple"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["white"],
            corner_radius=8, width=100, height=36,
            command=dlg.destroy,
        ).pack()

    def _show_completion_dialog(self, success: bool, message: str,
                                results: list[dict]):
        """Show results in a themed CTkToplevel dialog."""
        _DLG_W, _DLG_H = 440, 320

        dlg = ctk.CTkToplevel(self)
        dlg.title("整理完成" if success else "整理完成（有错误）")
        dlg.geometry(f"{_DLG_W}x{_DLG_H}")
        dlg.resizable(False, False)
        dlg.configure(fg_color=COLORS["bg_main"])
        dlg.transient(self)
        dlg.grab_set()
        self._center_dialog(dlg, _DLG_W, _DLG_H)

        # Title
        title_color = COLORS["success"] if success else COLORS["error"]
        title_text = "[OK] 整理完成" if success else "[!] 整理完成（有错误）"
        ctk.CTkLabel(
            dlg, text=title_text,
            font=ctk.CTkFont(family=FONT_FAMILY, size=16, weight="bold"),
            text_color=title_color,
        ).pack(pady=(20, 10))

        # Summary
        summary = f"成功处理 {len(results)} 个文件\n"
        # Group by podcast
        by_podcast = {}
        for r in results:
            by_podcast.setdefault(r["podcast"], []).append(r)
        for pname, items in by_podcast.items():
            summary += f"  {pname}: {len(items)} 个\n"

        ctk.CTkLabel(
            dlg, text=summary.strip(),
            font=ctk.CTkFont(family=FONT_FAMILY, size=13),
            text_color=COLORS["text_secondary"],
            justify="left",
        ).pack(pady=(0, 8))

        # Errors (if any)
        if not success:
            err_lines = message.split("\n")[-10:]  # Show last 10 error lines
            err_text = "\n".join(err_lines)
            if len(err_text) > 500:
                err_text = err_text[:500] + "\n..."
            ctk.CTkLabel(
                dlg, text=err_text,
                font=ctk.CTkFont(family=FONT_FAMILY, size=11),
                text_color=COLORS["error"],
                wraplength=_DLG_W - 48, justify="left",
            ).pack(pady=(0, 8))

        # Button row
        btn_frame = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_frame.pack(pady=(8, 0))

        ctk.CTkButton(
            btn_frame, text="打开播客目录",
            fg_color=COLORS["accent_purple"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["white"],
            corner_radius=8, width=150, height=36,
            command=lambda: (
                os.startfile(self._podcast_root), dlg.destroy()
            ) if self._podcast_root else dlg.destroy,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame, text="关闭",
            fg_color=COLORS["card_surface"],
            hover_color=COLORS["card_hover"],
            text_color=COLORS["text_primary"],
            border_color=COLORS["border_light"],
            border_width=1,
            corner_radius=8, width=100, height=36,
            command=dlg.destroy,
        ).pack(side="left", padx=8)

    def _show_exit_confirm(self):
        _DLG_W, _DLG_H = 360, 160
        dlg = ctk.CTkToplevel(self)
        dlg.title("确认退出")
        dlg.configure(fg_color=COLORS["bg_main"])
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()
        self._center_dialog(dlg, _DLG_W, _DLG_H)

        ctk.CTkLabel(
            dlg, text="正在处理中，确定要取消并退出吗?",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13),
            text_color=COLORS["text_primary"],
        ).pack(pady=(32, 20))

        btn_frame = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_frame.pack()

        ctk.CTkButton(
            btn_frame, text="确定退出",
            fg_color=COLORS["error"],
            text_color=COLORS["white"],
            corner_radius=8, width=100, height=36,
            command=lambda: (
                self._engine.cancel() if self._engine else None,
                dlg.destroy(),
                self.destroy(),
            ),
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame, text="取消",
            fg_color=COLORS["card_surface"],
            text_color=COLORS["text_primary"],
            border_color=COLORS["border_light"],
            border_width=1,
            corner_radius=8, width=100, height=36,
            command=dlg.destroy,
        ).pack(side="left", padx=8)

    def _center_dialog(self, dlg, w, h):
        dlg.update_idletasks()
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        dlg.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    def _on_close(self):
        if self._is_processing and self._engine:
            self._show_exit_confirm()
        else:
            self.destroy()

    # ==================================================================
    # Window sizing
    # ==================================================================

    def _center_window(self):
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        ww = max(680, min(1200, int(sw * 0.62)))
        wh = max(520, min(800, int(sh * 0.68)))
        x = (sw - ww) // 2
        y = (sh - wh) // 2
        self.geometry(f"{ww}x{wh}+{x}+{y}")
        self.minsize(680, 520)
        self.resizable(True, True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = PodTidyApp()
    app.mainloop()


if __name__ == "__main__":
    main()
