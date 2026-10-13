"""Windows 11 (Fluent, dark) look for Tkinter: colours, fonts and a few custom widgets.

Uses the sv_ttk theme for buttons, entries, switches and scrollbars when it is
installed, and draws the rest (cards, navigation, table rows) itself.
"""

import ctypes
import tkinter as tk
from tkinter import font as tkfont, ttk

BG = "#202020"           # window and sidebar
CONTENT = "#272727"      # page background
CARD = "#2b2b2b"
CARD_BORDER = "#1d1d1d"
HOVER = "#313131"
NAV_SEL = "#2d2d2d"
NAV_HOVER = "#2a2a2a"
LINE = "#383838"
FG, FG2, FG3 = "#ffffff", "#d0d0d0", "#9d9d9d"
FIELD = "#292929"        # sv_ttk entry field
ACCENT = "#60cdff"
OK, BAD, WARN = "#6ccb5f", "#ff99a4", "#fce100"

ICONS = {
    "menu": "", "grid": "", "list": "", "settings": "", "info": "",
    "search": "", "add": "", "refresh": "", "play": "", "star": "",
    "globe": "", "folder": "", "open": "", "cancel": "", "check": "",
}


class Fonts:
    def __init__(self, root):
        fams = set(tkfont.families(root))

        def pick(*names):
            return next((n for n in names if n in fams), "Segoe UI")
        text = pick("Segoe UI Variable Text", "Segoe UI")
        semibold = pick("Segoe UI Variable Text Semibold", "Segoe UI Semibold", "Segoe UI")
        display = pick("Segoe UI Variable Display Semib", "Segoe UI Semibold", "Segoe UI")
        small = pick("Segoe UI Variable Small", "Segoe UI")
        icons = pick("Segoe Fluent Icons", "Segoe MDL2 Assets")
        self.title = tkfont.Font(root, family=display, size=21)
        self.value = tkfont.Font(root, family=display, size=15)
        self.body = tkfont.Font(root, family=text, size=10)
        self.strong = tkfont.Font(root, family=semibold, size=10)
        self.section = tkfont.Font(root, family=semibold, size=11)
        self.small = tkfont.Font(root, family=small, size=9)
        self.icon = tkfont.Font(root, family=icons, size=12)
        self.icon_small = tkfont.Font(root, family=icons, size=10)
        self.mono = tkfont.Font(root, family="Consolas", size=9)


def apply_theme(root):
    """sv_ttk dark theme (falls back to a dark 'clam') plus styles for widgets that sit on cards."""
    style = ttk.Style(root)
    try:
        import sv_ttk
        sv_ttk.set_theme("dark")
        have_sv = True
    except Exception:
        style.theme_use("clam")
        style.configure(".", background=CONTENT, foreground=FG, fieldbackground="#1f1f1f",
                        bordercolor=LINE, lightcolor=CARD, darkcolor=CARD)
        style.configure("TButton", background="#373737", padding=(12, 5))
        style.map("TButton", background=[("active", "#404040"), ("disabled", "#2e2e2e")],
                  foreground=[("disabled", FG3)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#000000")
        style.map("Accent.TButton", background=[("active", "#7fd7ff"), ("disabled", "#3a3a3a")])
        style.configure("Switch.TCheckbutton", background=CARD)
        have_sv = False
    root.configure(bg=BG)
    for prefix, bg in (("Page", CONTENT), ("Card", CARD), ("Side", BG)):
        for base in ("TButton", "Accent.TButton", "TCheckbutton", "Switch.TCheckbutton", "TRadiobutton",
                     "TLabel", "TFrame", "TEntry", "TCombobox"):
            style.configure("%s.%s" % (prefix, base), background=bg)
        style.configure("%s.TCheckbutton" % prefix, foreground=FG)
        style.configure("%s.TRadiobutton" % prefix, foreground=FG)
        style.configure("%s.Switch.TCheckbutton" % prefix, foreground=FG)
    style.configure("Hover.TButton", background=HOVER)
    style.configure("Thin.Horizontal.TProgressbar", thickness=4)
    return have_sv


def dark_title_bar(root):
    """Dark native title bar on Windows 10/11, coloured like the sidebar on Windows 11."""
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)   # immersive dark mode
        col = ctypes.c_int(0x00202020)                              # COLORREF 0x00BBGGRR
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(col), 4)  # caption colour (Win 11)
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(col), 4)  # border colour (Win 11)
    except Exception:
        pass


def rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class Card(tk.Canvas):
    """Rounded card hosting `self.body` (a Frame with the card colour). The card grows to fit
    its body unless `fill=True`, in which case the body stretches to the card's size."""

    def __init__(self, parent, bg=CONTENT, color=CARD, border=CARD_BORDER, radius=8, pad=(16, 14), fill=False):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0, height=40)
        self.color, self.border, self.radius, self.pad, self.fill = color, border, radius, pad, fill
        self.body = tk.Frame(self, bg=color)
        self._win = self.create_window(pad[0], pad[1], window=self.body, anchor="nw")
        self.bind("<Configure>", self._redraw)
        if not fill:
            self.body.bind("<Configure>", self._fit)

    def _fit(self, _e=None):
        h = self.body.winfo_reqheight() + 2 * self.pad[1]
        if int(self.cget("height")) != h:
            self.configure(height=h)

    def _redraw(self, _e=None):
        w, h = self.winfo_width(), self.winfo_height()
        self.delete("shape")
        rounded_rect(self, 0, 0, w - 1, h - 1, self.radius, fill=self.color, outline=self.border, tags="shape")
        self.tag_lower("shape")
        self.itemconfigure(self._win, width=max(1, w - 2 * self.pad[0]))
        if self.fill:
            self.itemconfigure(self._win, height=max(1, h - 2 * self.pad[1]))


class Link(tk.Label):
    def __init__(self, parent, text, command, fonts, bg=CARD):
        super().__init__(parent, text=text, fg=ACCENT, bg=bg, font=fonts.small, cursor="hand2")
        self._command = command
        self.bind("<Button-1>", lambda e: self._command())
        self.bind("<Enter>", lambda e: self.configure(fg="#99ebff"))
        self.bind("<Leave>", lambda e: self.configure(fg=ACCENT))


class Tile(tk.Canvas):
    """Small rounded square with a two-letter tag, like a game icon."""

    def __init__(self, parent, text, color, fonts, bg=CARD, size=32):
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        rounded_rect(self, 0, 0, size - 1, size - 1, 6, fill=color, outline=color)
        self.create_text(size / 2, size / 2, text=text, fill="#101010", font=fonts.strong)

    def set_bg(self, color):
        self.configure(bg=color)


class NavItem(tk.Frame):
    def __init__(self, parent, icon, text, command, fonts):
        super().__init__(parent, bg=BG, height=40, cursor="hand2")
        self.pack_propagate(False)
        self.selected = False
        self.pill = tk.Canvas(self, width=3, height=16, bg=BG, highlightthickness=0)
        self.pill.pack(side="left", padx=(4, 0))
        self.icon = tk.Label(self, text=ICONS[icon], font=fonts.icon, fg=FG, bg=BG)
        self.icon.pack(side="left", padx=(10, 14))
        self.label = tk.Label(self, text=text, font=fonts.body, fg=FG, bg=BG, anchor="w")
        self.label.pack(side="left", fill="x", expand=True)
        for w in (self, self.pill, self.icon, self.label):
            w.bind("<Button-1>", lambda e: command())
            w.bind("<Enter>", lambda e: self._paint(True))
            w.bind("<Leave>", lambda e: self._paint(False))

    def _paint(self, hover=False):
        bg = NAV_SEL if self.selected else (NAV_HOVER if hover else BG)
        for w in (self, self.pill, self.icon, self.label):
            w.configure(bg=bg)
        self.pill.delete("all")
        if self.selected:
            rounded_rect(self.pill, 0, 0, 2, 15, 1, fill=ACCENT, outline=ACCENT)

    def select(self, on):
        self.selected = on
        self._paint()

    def compact(self, on):
        if on:
            self.label.pack_forget()
        else:
            self.label.pack(side="left", fill="x", expand=True)


class Scroll(tk.Frame):
    """Vertically scrolling area; put widgets in `self.inner`."""

    def __init__(self, parent, bg=CARD):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _wheel(self, e):
        if self.inner.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-e.delta / 120) * 3, "units")

    def to_top(self):
        self.canvas.yview_moveto(0)


class SearchBox(tk.Frame):
    """Entry with a placeholder and a magnifier, like the WinUI AutoSuggestBox."""

    def __init__(self, parent, var, placeholder, fonts, bg=CARD, width=36):
        super().__init__(parent, bg=bg)
        self.var = var
        self.entry = ttk.Entry(self, textvariable=var, width=width)
        self.entry.pack(side="left")
        self.hint = tk.Label(self.entry, text=placeholder, fg=FG3, bg=FIELD, font=fonts.body, cursor="xterm")
        self.icon = tk.Label(self.entry, text=ICONS["search"], fg=FG2, bg=FIELD, font=fonts.icon_small)
        self.icon.place(relx=1.0, x=-12, rely=0.5, anchor="e")
        self.hint.bind("<Button-1>", lambda e: self.entry.focus_set())
        var.trace_add("write", lambda *a: self._update())
        self.entry.bind("<FocusIn>", lambda e: self._update())
        self.entry.bind("<FocusOut>", lambda e: self._update())
        self.after(50, self._update)

    def _update(self):
        if self.var.get():
            self.hint.place_forget()
        else:
            self.hint.place(x=11, rely=0.5, anchor="w")


def elide(text, n):
    return text if len(text) <= n else "…" + text[-(n - 1):]
