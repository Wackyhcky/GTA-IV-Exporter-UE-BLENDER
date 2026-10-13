"""GTA IV Exporter - desktop app.

Pick your GTA IV folder and the districts you want, and it builds either a
.blend file for Blender or a level inside an Unreal Engine project.
No OpenIV needed.
"""

import glob
import json
import os
import queue
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# When frozen by PyInstaller, the runner scripts and the add-on package are
# bundled as plain files (Blender's / Unreal's own Python imports them from there).
HERE = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

APP_NAME = "GTA IV Exporter"
_APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
SETTINGS_DIR = os.path.join(_APPDATA, "GTAIVExporter")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
_OLD_SETTINGS_DIR = os.path.join(_APPDATA, "GTA4MapExporter")  # before the rename to GTA IV Exporter
if not os.path.isdir(SETTINGS_DIR) and os.path.isdir(_OLD_SETTINGS_DIR):
    try:
        shutil.copytree(_OLD_SETTINGS_DIR, SETTINGS_DIR)  # keeps the settings and the cached game key
    except OSError:
        pass
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# area prefix -> island / borough name
GROUPS = [
    ("Algonquin", ("manhat",)),
    ("Alderney", ("nj_",)),
    ("Broker", ("brook_",)),
    ("Dukes", ("queens_",)),
    ("Bohan", ("bronx_",)),
    ("Other", ("",)),
]
FULL_MAP_PLACEMENTS = 165857
# measured triangle share of each detail level vs full detail (manhat01 / bronx_e)
DETAIL_SHARE = {"FULL": 1.0, "HIGH": 0.7, "MEDIUM": 0.35, "LOW": 0.17, "VERYLOW": 0.08}
# Unreal import: share of the progress bar for the conversion step, mesh batches
# (150 models each) per editor process, and crashed attempts in a row before giving up
UE_CONVERT_SHARE = 0.3
UE_BATCHES_PER_PASS = 12
UE_MAX_RETRIES = 4
# measured: full map in Blender ~2.7 GB textures + 0.85 GB .blend;
# Unreal ~0.72 GB (export + imported assets) for manhat01, scaled to the full map
FULL_MAP_BYTES = {"BLENDER": 3.6e9, "UNREAL": 9e9}


def group_of(area):
    for name, prefixes in GROUPS:
        if any(area.startswith(p) for p in prefixes):
            return name
    return "Other"


def natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


# ---------------------------------------------------------------- detection
def steam_libraries():
    roots = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            roots.append(winreg.QueryValueEx(k, "SteamPath")[0])
    except Exception:
        pass
    roots.append(r"C:\Program Files (x86)\Steam")
    libs = []
    for r in roots:
        vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
        if os.path.isfile(vdf):
            try:
                text = open(vdf, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                p = m.group(1).replace("\\\\", "\\")
                if p not in libs:
                    libs.append(p)
    return libs


def find_game_dir():
    cands = [os.path.join(l, "steamapps", "common", "Grand Theft Auto IV", "GTAIV") for l in steam_libraries()]
    cands += [r"C:\Program Files\Rockstar Games\Grand Theft Auto IV",
              r"C:\Program Files (x86)\Rockstar Games\Grand Theft Auto IV",
              r"C:\Program Files\Rockstar Games\Grand Theft Auto IV The Complete Edition"]
    for c in cands:
        if os.path.isfile(os.path.join(c, "GTAIV.exe")):
            return c
    return ""


def _ver(p):
    m = re.search(r"(?:Blender|UE_)\s*(\d+)\.(\d+)", p)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def find_blender():
    found = glob.glob(r"C:\Program Files\Blender Foundation\Blender*\blender.exe")
    for l in steam_libraries():
        p = os.path.join(l, "steamapps", "common", "Blender", "blender.exe")
        if os.path.isfile(p):
            found.append(p)
    w = shutil.which("blender")
    if w:
        found.append(w)
    found.sort(key=_ver, reverse=True)
    return found[0] if found else ""


def unreal_installs():
    """{'5.8': 'C:\\Program Files\\Epic Games\\UE_5.8', ...} from the Epic launcher + common folders."""
    out = {}
    dat = r"C:\ProgramData\Epic\UnrealEngineLauncher\LauncherInstalled.dat"
    try:
        for e in json.load(open(dat, encoding="utf-8")).get("InstallationList", []):
            if e.get("AppName", "").startswith("UE_"):
                out[e["AppName"][3:]] = e["InstallLocation"]
    except Exception:
        pass
    for d in glob.glob(r"C:\Program Files\Epic Games\UE_*") + glob.glob(r"?:\Epic Games\UE_*"):
        out.setdefault(os.path.basename(d)[3:], d)
    return {k: v for k, v in out.items()
            if os.path.isfile(os.path.join(v, "Engine", "Binaries", "Win64", "UnrealEditor.exe"))}


def find_unreal(uproject=None):
    installs = unreal_installs()
    if uproject and os.path.isfile(uproject):
        try:
            assoc = json.load(open(uproject, encoding="utf-8")).get("EngineAssociation", "")
            if assoc in installs:
                return os.path.join(installs[assoc], "Engine", "Binaries", "Win64", "UnrealEditor.exe")
        except Exception:
            pass
    if not installs:
        return ""
    best = max(installs, key=lambda v: _ver("UE_" + v))
    return os.path.join(installs[best], "Engine", "Binaries", "Win64", "UnrealEditor.exe")


def unreal_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq UnrealEditor.exe", "/NH"],
                             capture_output=True, text=True, creationflags=NO_WINDOW).stdout
        return "UnrealEditor.exe" in out
    except Exception:
        return False


def default_output():
    best, best_free = None, -1
    for d in string.ascii_uppercase:
        root = d + ":\\"
        if not os.path.exists(root):
            continue
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            continue
        if free > best_free:
            best, best_free = root, free
    base = best or os.path.expanduser("~")
    return os.path.join(base, "GTA IV Blender", "liberty_city.blend")


def bundled_traffic_plugin():
    """The compiled GTAIVTraffic plugin shipped with the app (built for Unreal 5.8)."""
    for rel in (("UnrealPlugin", "GTAIVTraffic"), ("UnrealPlugin", "GTAIVTraffic_Built")):
        d = os.path.join(HERE, *rel)
        if os.path.isfile(os.path.join(d, "GTAIVTraffic.uplugin")) and os.path.isdir(os.path.join(d, "Binaries")):
            return d
    return None


def install_traffic_plugin(uproject, log):
    """Copy the plugin into <project>/Plugins and enable it in the .uproject (backed up first)."""
    src = bundled_traffic_plugin()
    if src is None:
        log("Traffic plugin not found next to the app; skipping traffic")
        return False
    dest = os.path.join(os.path.dirname(uproject), "Plugins", "GTAIVTraffic")
    dll = os.path.join("Binaries", "Win64", "UnrealEditor-GTAIVTraffic.dll")
    same = (os.path.isfile(os.path.join(dest, dll)) and
            os.path.getsize(os.path.join(dest, dll)) == os.path.getsize(os.path.join(src, dll)) and
            open(os.path.join(dest, dll), "rb").read() == open(os.path.join(src, dll), "rb").read())
    if not same:
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        log("Installed the GTAIVTraffic plugin into %s" % os.path.dirname(dest))
    proj = json.load(open(uproject, encoding="utf-8"))
    plugins = proj.setdefault("Plugins", [])
    entry = next((pl for pl in plugins if pl.get("Name") == "GTAIVTraffic"), None)
    if entry is None or not entry.get("Enabled", False):
        shutil.copy2(uproject, uproject + ".bak")
        if entry is None:
            plugins.append({"Name": "GTAIVTraffic", "Enabled": True})
        else:
            entry["Enabled"] = True
        with open(uproject, "w", encoding="utf-8") as f:
            json.dump(proj, f, indent="\t")
        log("Enabled GTAIVTraffic in %s (backup: .uproject.bak)" % os.path.basename(uproject))
    return True


def load_settings():
    try:
        return json.load(open(SETTINGS_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass  # e.g. system drive full; settings are a convenience


# ---------------------------------------------------------------- app
from fluent import (ACCENT, BAD, BG, CARD, CONTENT, FG, FG2, FG3, HOVER, ICONS, LINE, OK, Card, Fonts, Link,
                    NavItem, Scroll, SearchBox, Tile, apply_theme, dark_title_bar, elide)

GROUP_STYLE = {  # borough -> (tile tag, tile colour)
    "Algonquin": ("AL", "#f0b04a"), "Alderney": ("AD", "#c38af0"), "Broker": ("BR", "#4cc3d4"),
    "Dukes": ("DU", "#7fcf6c"), "Bohan": ("BO", "#f08a8a"), "Other": ("··", "#a0a0a0"),
}
# table column widths (px): tile, name, placements, models, LODs, status, action
COLS = (44, 0, 110, 90, 80, 210, 110)
EXTRAS = (  # (setting, label, applies to Blender)
    ("textures", "Textures", True),
    ("interiors", "Interiors", True),
    ("lights", "Lights", True),
    ("traffic", "Traffic", False),
    ("characters", "Characters & weapons", False),
)


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def is_game_dir(d):
    return bool(d) and os.path.isfile(os.path.join(d, "GTAIV.exe"))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1320x900")
        self.minsize(1120, 720)
        self.q = queue.Queue()
        self.proc = None
        self.cancelled = False
        self.area_counts = {}
        self.area_vars = {}
        self.rows = {}
        self.start_time = None
        self._result = None
        self._compact = False

        s = load_settings()
        self.target = tk.StringVar(value=s.get("target", "BLENDER"))
        self.game_dir = tk.StringVar(value=s.get("game_dir") or find_game_dir())
        self.blender = tk.StringVar(value=s.get("blender") or find_blender())
        self.output = tk.StringVar(value=s.get("output") or default_output())
        self.uproject = tk.StringVar(value=s.get("uproject", ""))
        self.unreal = tk.StringVar(value=s.get("unreal") or find_unreal(s.get("uproject")))
        self.map_name = tk.StringVar(value=s.get("map_name", "LibertyCity"))
        self.mode = tk.StringVar(value=s.get("mode", "INSTANCES"))
        self.ue_mode = tk.StringVar(value=s.get("ue_mode", "INSTANCES"))
        self.detail = tk.StringVar(value=s.get("detail") or ("LOW" if s.get("lods") else "FULL"))
        self.textures = tk.BooleanVar(value=s.get("textures", True))
        self.normals = tk.BooleanVar(value=s.get("normals", True))
        self.interiors = tk.BooleanVar(value=s.get("interiors", True))
        self.lights = tk.BooleanVar(value=s.get("lights", True))
        self.traffic = tk.BooleanVar(value=s.get("traffic", True))
        self.characters = tk.BooleanVar(value=s.get("characters", False))
        self.nanite = tk.BooleanVar(value=s.get("nanite", False))
        self.collision = tk.BooleanVar(value=s.get("collision", True))
        self.keep_files = tk.BooleanVar(value=s.get("keep_files", False))
        self.search = tk.StringVar()
        self._saved_areas = set(s.get("areas", []))

        from gtaiv_exporter.mapdata import DETAIL_LEVELS
        self._detail_levels = DETAIL_LEVELS
        self.f = Fonts(self)
        apply_theme(self)
        self._build()
        dark_title_bar(self)
        for v in (self.game_dir, self.blender, self.unreal, self.uproject, self.output, self.map_name):
            v.trace_add("write", lambda *a: self._check_paths())
        for v in (self.textures, self.interiors, self.lights, self.traffic, self.characters, self.detail,
                  self.mode, self.ue_mode):
            v.trace_add("write", lambda *a: self._update_summary())
        self.target.trace_add("write", lambda *a: self._target_changed())
        self.search.trace_add("write", lambda *a: self._filter_rows())
        self._target_changed()
        self._show("districts")
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if is_game_dir(self.game_dir.get()):
            self.after(200, self.scan)

    # ------------------------------------------------------------ small builders
    def _label(self, parent, text="", font=None, fg=FG, bg=CARD, **kw):
        return tk.Label(parent, text=text, font=font or self.f.body, fg=fg, bg=bg, anchor="w", justify="left", **kw)

    def _page_header(self, page, title):
        head = tk.Frame(page, bg=CONTENT)
        head.pack(fill="x", padx=36, pady=(30, 0))
        tk.Label(head, text=title, font=self.f.title, fg=FG, bg=CONTENT).pack(side="left")
        sub = tk.Label(head, text="", font=self.f.body, fg=FG2, bg=CONTENT)
        sub.pack(side="left", padx=(14, 0), pady=(10, 0))
        return sub

    def _button_bar(self, page):
        bar = tk.Frame(page, bg=CONTENT)
        bar.pack(fill="x", padx=36, pady=(14, 0))
        inner = tk.Frame(bar, bg=CONTENT)
        inner.pack(side="right")
        return inner

    def _btn(self, parent, text, command, accent=False, bg="Page", icon=None):
        label = ("%s  %s" % (ICONS[icon], text)) if icon else text
        return ttk.Button(parent, text=label, command=command,
                          style="%s.%s" % (bg, "Accent.TButton" if accent else "TButton"))

    def _stat_card(self, parent, column, title, change):
        card = Card(parent)
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 6, 0 if column == 3 else 6))
        self._label(card.body, title, fg=FG).pack(anchor="w")
        value = tk.Frame(card.body, bg=CARD)
        value.pack(anchor="w", pady=(6, 0))
        big = self._label(value, "", font=self.f.value)
        big.pack(side="left")
        unit = self._label(value, "", font=self.f.body, fg=FG2)
        unit.pack(side="left", padx=(6, 0), pady=(5, 0))
        bar = tk.Canvas(card.body, height=3, bg=CARD, highlightthickness=0)
        note = self._label(card.body, "", font=self.f.small, fg=FG2, wraplength=250)
        note.pack(anchor="w", pady=(6, 0), fill="x")
        card.body.bind("<Configure>", lambda e: note.configure(wraplength=max(80, e.width - 4)), add="+")
        Link(card.body, "Change", change, self.f).pack(anchor="w", pady=(10, 0))
        return {"big": big, "unit": unit, "note": note, "bar": bar}

    def _settings_card(self, parent, title, subtitle=None):
        card = Card(parent)
        card.pack(fill="x", padx=36, pady=(0, 12))
        self._label(card.body, title, font=self.f.section).pack(anchor="w")
        if subtitle:
            self._label(card.body, subtitle, font=self.f.small, fg=FG3).pack(anchor="w", pady=(2, 0))
        body = tk.Frame(card.body, bg=CARD)
        body.pack(fill="x", pady=(10, 0))
        body.columnconfigure(1, weight=1)
        return body

    def _path_row(self, parent, row, label, var, browse):
        self._label(parent, label, fg=FG2).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=4)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", pady=4)
        self._btn(parent, "Browse…", browse, bg="Card").grid(row=row, column=2, padx=(8, 0), pady=4)
        st = self._label(parent, "", font=self.f.small, width=24)
        st.grid(row=row, column=3, sticky="w", padx=(12, 0))
        return st

    def _switches(self, parent, row, items):
        sw = tk.Frame(parent, bg=CARD)
        sw.grid(row=row, column=0, columnspan=4, sticky="w", pady=(10, 0))
        for i, (text, var) in enumerate(items):
            ttk.Checkbutton(sw, text=text, variable=var, style="Card.Switch.TCheckbutton").grid(
                row=i // 3, column=i % 3, sticky="w", padx=(0, 28), pady=(0 if i < 3 else 6, 0))
        return sw

    # ------------------------------------------------------------ layout
    def _build(self):
        # title strip (hamburger, app glyph, name) like WinUI apps with a custom title bar
        top = tk.Frame(self, bg=BG, height=44)
        top.pack(fill="x")
        ham = tk.Label(top, text=ICONS["menu"], font=self.f.icon, fg=FG, bg=BG, cursor="hand2")
        ham.pack(side="left", padx=(18, 14), pady=10)
        ham.bind("<Button-1>", lambda e: self._toggle_sidebar())
        tk.Label(top, text=ICONS["globe"], font=self.f.icon, fg=ACCENT, bg=BG).pack(side="left", padx=(0, 10))
        tk.Label(top, text="GTA IV", font=self.f.strong, fg=FG, bg=BG).pack(side="left")
        tk.Label(top, text="for Blender & Unreal", font=self.f.small, fg=FG2, bg=BG).pack(
            side="left", padx=(10, 0))

        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True)

        # sidebar
        self.side = tk.Frame(main, bg=BG, width=250)
        self.side.pack(side="left", fill="y")
        self.side.pack_propagate(False)
        self.nav = {
            "districts": NavItem(self.side, "grid", "Districts", lambda: self._show("districts"), self.f),
            "export": NavItem(self.side, "list", "Export", lambda: self._show("export"), self.f),
            "settings": NavItem(self.side, "settings", "Settings", lambda: self._show("settings"), self.f),
        }
        for n in self.nav.values():
            n.pack(fill="x", padx=6, pady=1)
        about = NavItem(self.side, "info", "About", self._about, self.f)
        about.pack(side="bottom", fill="x", padx=6, pady=(4, 10))
        self.about_item = about
        self.game_card = Card(self.side, bg=BG, pad=(12, 10))
        self.game_card.pack(side="bottom", fill="x", padx=6, pady=(0, 2))
        self._label(self.game_card.body, "GAME", font=self.f.small, fg=FG2).pack(anchor="w")
        self.game_name = self._label(self.game_card.body, "Grand Theft Auto IV", font=self.f.strong)
        self.game_name.pack(anchor="w", pady=(2, 0))
        self.game_info = self._label(self.game_card.body, "", font=self.f.small, fg=FG2)
        self.game_info.pack(anchor="w")

        # content area (rounded top-left like WinUI's NavigationView content)
        content = Card(main, bg=BG, color=CONTENT, border="#1a1a1a", radius=8, pad=(0, 0), fill=True)
        content.pack(side="left", fill="both", expand=True)
        holder = content.body
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)
        self.pages = {}
        for name, build in (("districts", self._build_districts), ("export", self._build_export),
                            ("settings", self._build_settings)):
            page = tk.Frame(holder, bg=CONTENT)
            page.grid(row=0, column=0, sticky="nsew")
            build(page)
            self.pages[name] = page

    def _toggle_sidebar(self):
        self._compact = not self._compact
        self.side.configure(width=60 if self._compact else 250)
        for n in list(self.nav.values()) + [self.about_item]:
            n.compact(self._compact)
        if self._compact:
            self.game_card.pack_forget()
        else:
            self.game_card.pack(side="bottom", fill="x", padx=6, pady=(0, 2), after=self.about_item)

    def _show(self, name):
        self.pages[name].tkraise()
        for k, n in self.nav.items():
            n.select(k == name)

    def _about(self):
        messagebox.showinfo(APP_NAME, "%s\n\nExports Liberty City straight from your GTA IV install to Blender "
                                      "or Unreal Engine: the map, interiors, lights, traffic and characters. "
                                      "No OpenIV needed.\n\nBy dessierror." % APP_NAME)

    # ------------------------------------------------------------ districts page
    def _build_districts(self, page):
        self.dist_sub = self._page_header(page, "Districts")
        bar = self._button_bar(page)
        self.rescan_btn = self._btn(bar, "Rescan", self.scan, icon="refresh")
        self.rescan_btn.pack(side="left", padx=(0, 8))
        self.all_btn = self._btn(bar, "Select all", lambda: self._select_all(True), icon="add")
        self.all_btn.pack(side="left", padx=(0, 8))
        self._btn(bar, "Select none", lambda: self._select_all(False)).pack(side="left", padx=(0, 8))
        self.export_btn2 = self._btn(bar, "Export", self.export, accent=True, icon="play")
        self.export_btn2.pack(side="left")

        cards = tk.Frame(page, bg=CONTENT)
        cards.pack(fill="x", padx=36, pady=(18, 0))
        for i in range(4):
            cards.columnconfigure(i, weight=1, uniform="cards")
        self.card_disk = self._stat_card(cards, 0, "Disk space", lambda: self._show("settings"))
        self.card_disk["bar"].pack(fill="x", pady=(8, 0), before=self.card_disk["note"])
        self.card_target = self._stat_card(cards, 1, "Export to", lambda: self._show("settings"))
        self.card_detail = self._stat_card(cards, 2, "Map detail", self._detail_menu)
        self.card_extras = self._stat_card(cards, 3, "World", self._extras_menu)

        table = Card(page, fill=True, pad=(16, 14))
        table.pack(fill="both", expand=True, padx=36, pady=(20, 24))
        tb = table.body
        top = tk.Frame(tb, bg=CARD)
        top.pack(fill="x")
        SearchBox(top, self.search, "Search by district or borough", self.f).pack(side="left")
        head = tk.Frame(tb, bg=CARD)
        head.pack(fill="x", pady=(14, 4))
        self._grid_cols(head)
        for col, text in ((1, "District"), (2, "Placements"), (3, "Models"), (4, "LODs"), (5, "Status")):
            self._label(head, text, font=self.f.small, fg=FG2).grid(row=0, column=col, sticky="w")
        tk.Frame(tb, bg=LINE, height=1).pack(fill="x")
        self.table = Scroll(tb)
        self.table.pack(fill="both", expand=True)
        self.empty = self._label(self.table.inner, "Choose your GTA IV folder in Settings to list districts.", fg=FG3)
        self.empty.pack(anchor="w", pady=20, padx=4)

    @staticmethod
    def _grid_cols(frame):
        for i, w in enumerate(COLS):
            frame.columnconfigure(i, minsize=w, weight=1 if w == 0 else 0)

    def _detail_menu(self):
        m = tk.Menu(self, tearoff=0)
        for key, label, _ in self._detail_levels:
            m.add_radiobutton(label=label, variable=self.detail, value=key)
        m.tk_popup(*self.winfo_pointerxy())

    def _extras_menu(self):
        m = tk.Menu(self, tearoff=0)
        ue = self.target.get() == "UNREAL"
        for key, label, blender in EXTRAS:
            m.add_checkbutton(label=label + ("" if blender else "  (Unreal)"), variable=getattr(self, key),
                              state="normal" if (ue or blender) else "disabled")
        m.tk_popup(*self.winfo_pointerxy())

    def _detail_parts(self):
        label = next((l for k, l, _ in self._detail_levels if k == self.detail.get()), self._detail_levels[0][1])
        name, _, rest = label.partition(" (")
        return name, rest.rstrip(")")

    # ------------------------------------------------------------ export page
    def _build_export(self, page):
        self.exp_sub = self._page_header(page, "Export")
        bar = self._button_bar(page)
        self.folder_btn = self._btn(bar, "Open folder", self.open_folder, icon="folder")
        self.folder_btn.pack(side="left", padx=(0, 8))
        self.open_btn = self._btn(bar, "Open result", self.open_result, icon="open")
        self.open_btn.pack(side="left", padx=(0, 8))
        self.open_btn.state(["disabled"])
        self.cancel_btn = self._btn(bar, "Cancel", self.cancel, icon="cancel")
        self.cancel_btn.pack(side="left", padx=(0, 8))
        self.cancel_btn.state(["disabled"])
        self.export_btn = self._btn(bar, "Export", self.export, accent=True, icon="play")
        self.export_btn.pack(side="left")

        row = tk.Frame(page, bg=CONTENT)
        row.pack(fill="x", padx=36, pady=(18, 0))
        row.columnconfigure(0, weight=3, uniform="exp")
        row.columnconfigure(1, weight=2, uniform="exp")
        prog = Card(row)
        prog.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self._label(prog.body, "Progress").pack(anchor="w")
        self.pct = self._label(prog.body, "—", font=self.f.value)
        self.pct.pack(anchor="w", pady=(6, 0))
        self.progress = ttk.Progressbar(prog.body, mode="determinate", maximum=1000)
        self.progress.pack(fill="x", pady=(10, 8))
        self.status = self._label(prog.body, "Ready.", font=self.f.small, fg=FG2, wraplength=560)
        self.status.pack(anchor="w", fill="x")
        summ = Card(row)
        summ.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self._label(summ.body, "This export").pack(anchor="w")
        grid = tk.Frame(summ.body, bg=CARD)
        grid.pack(fill="x", pady=(8, 0))
        self.summary_vals = {}
        for i, key in enumerate(("Target", "Districts", "Placements", "Detail", "World", "Output")):
            self._label(grid, key, font=self.f.small, fg=FG3).grid(row=i, column=0, sticky="w", padx=(0, 16), pady=1)
            v = self._label(grid, "", font=self.f.small)
            v.grid(row=i, column=1, sticky="w", pady=1)
            self.summary_vals[key] = v

        logc = Card(page, fill=True)
        logc.pack(fill="both", expand=True, padx=36, pady=(12, 24))
        self._label(logc.body, "Log").pack(anchor="w", pady=(0, 8))
        lf = tk.Frame(logc.body, bg=CARD)
        lf.pack(fill="both", expand=True)
        self.log = tk.Text(lf, wrap="word", font=self.f.mono, state="disabled", bg="#232323", fg=FG2,
                           insertbackground=FG, relief="flat", bd=0, padx=10, pady=8, highlightthickness=0)
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

    # ------------------------------------------------------------ settings page
    def _build_settings(self, page):
        self._page_header(page, "Settings").configure(text="Where things are and how the map is exported")
        sc = Scroll(page, bg=CONTENT)
        sc.pack(fill="both", expand=True, pady=(18, 0))
        p = sc.inner

        g = self._settings_card(p, "GTA IV", "The folder that contains GTAIV.exe (Steam, Rockstar or "
                                             "Complete Edition). No OpenIV needed.")
        self.game_status = self._path_row(g, 0, "Game folder", self.game_dir, self._browse_game)

        t = self._settings_card(p, "Export to")
        ttk.Radiobutton(t, text="Blender (.blend file)", variable=self.target, value="BLENDER",
                        style="Card.TRadiobutton").grid(row=0, column=0, sticky="w", padx=(0, 24))
        ttk.Radiobutton(t, text="Unreal Engine (level in a project)", variable=self.target, value="UNREAL",
                        style="Card.TRadiobutton").grid(row=0, column=1, sticky="w")

        b = self._settings_card(p, "Blender", "Needs Blender 4.2 or newer.")
        self.blender_status = self._path_row(b, 0, "Blender", self.blender, self._browse_blender)
        self.output_status = self._path_row(b, 1, "Save .blend as", self.output, self._browse_output)
        opts = tk.Frame(b, bg=CARD)
        opts.grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Radiobutton(opts, text="Geometry Nodes instances (fast, recommended for big exports)", variable=self.mode,
                        value="INSTANCES", style="Card.TRadiobutton").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(opts, text="Separate objects (editable, best for a few districts)", variable=self.mode,
                        value="OBJECTS", style="Card.TRadiobutton").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._switches(b, 3, (("Game normals", self.normals),))

        u = self._settings_card(p, "Unreal Engine", "Close Unreal Editor before exporting. Everything goes into "
                                                   "Content/GTAIV in the project.")
        self.uproject_status = self._path_row(u, 0, "Unreal project", self.uproject, self._browse_uproject)
        self.unreal_status = self._path_row(u, 1, "Unreal Editor", self.unreal, self._browse_unreal)
        self._label(u, "Level name", fg=FG2).grid(row=2, column=0, sticky="w", padx=(0, 16), pady=4)
        lvl = tk.Frame(u, bg=CARD)
        lvl.grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Entry(lvl, textvariable=self.map_name, width=28).pack(side="left")
        self.map_hint = self._label(lvl, "", font=self.f.small, fg=FG3)
        self.map_hint.pack(side="left", padx=(12, 0))
        self.ue_space_status = self._label(u, "", font=self.f.small, width=24)
        self.ue_space_status.grid(row=2, column=3, sticky="w", padx=(12, 0))
        uo = tk.Frame(u, bg=CARD)
        uo.grid(row=3, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Radiobutton(uo, text="Instanced meshes, one actor per district (fast, recommended)", variable=self.ue_mode,
                        value="INSTANCES", style="Card.TRadiobutton").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(uo, text="Separate actors (editable, best for a few districts)", variable=self.ue_mode,
                        value="ACTORS", style="Card.TRadiobutton").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._switches(u, 4, (("Collision (walkable)", self.collision), ("Nanite", self.nanite),
                              ("Keep intermediate files", self.keep_files),
                              ("Traffic (cars driving the roads)", self.traffic),
                              ("Characters & weapons", self.characters)))
        self._label(u, "Characters & weapons: all 345 peds plus Niko and 79 weapons. In a Game Animation Sample "
                       "project they also walk the sidewalks, and you play as Niko.", font=self.f.small, fg=FG3,
                    wraplength=820).grid(row=5, column=0, columnspan=4, sticky="w", pady=(8, 0))

        d = self._settings_card(p, "Map detail", "Less detail runs faster (e.g. in skate). GTA's own LOD models "
                                                "replace the full ones at the lower levels.")
        for i, (key, label, _) in enumerate(self._detail_levels):
            name, _, rest = label.partition(" (")
            ttk.Radiobutton(d, text=name, variable=self.detail, value=key, style="Card.TRadiobutton").grid(
                row=2 * i, column=0, sticky="w", pady=(6 if i else 0, 0))
            share = DETAIL_SHARE.get(key, 1.0)
            desc = rest.rstrip(")") + ("" if share == 1.0 else " · about %d%% of the triangles" % round(share * 100))
            self._label(d, desc[:1].upper() + desc[1:], font=self.f.small, fg=FG3, wraplength=820).grid(
                row=2 * i + 1, column=0, columnspan=4, sticky="w", padx=(30, 0))

        w = self._settings_card(p, "World", "What else comes along with the buildings and streets.")
        self._switches(w, 0, (("Textures", self.textures), ("Interiors", self.interiors),
                              ("Lights (street lamps, windows, day/night)", self.lights)))
        tk.Frame(p, bg=CONTENT, height=12).pack()

    # ------------------------------------------------------------ state
    def _target_changed(self):
        ue = self.target.get() == "UNREAL"
        for b in (self.export_btn, self.export_btn2):
            b.configure(text="%s  %s" % (ICONS["play"], "Export to Unreal" if ue else "Export to Blender"))
        self.open_btn.configure(text="%s  %s" % (ICONS["open"], "Open in Unreal" if ue else "Open in Blender"))
        if ue and os.path.isfile(self._map_file()) and not self._running():
            self.open_btn.state(["!disabled"])
        self._check_paths()

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, label, ok, text):
        label.configure(text=("✔  " if ok else "✖  ") + text, fg=OK if ok else BAD)

    def _map_name_ok(self):
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", self.map_name.get().strip()))

    def _check_paths(self):
        if not hasattr(self, "game_status"):
            return False, False
        g_ok = is_game_dir(self.game_dir.get())
        self._set_status(self.game_status, g_ok, "GTAIV.exe found" if g_ok else "GTAIV.exe not found")
        self._update_game_card(g_ok)
        up = self.uproject.get()
        p_ok = up.lower().endswith(".uproject") and os.path.isfile(up)
        self._set_status(self.uproject_status, p_ok, "project found" if p_ok else "pick a .uproject")
        ue = self.unreal.get()
        u_ok = os.path.isfile(ue) and os.path.isfile(self._unreal_cmd())
        self._set_status(self.unreal_status, u_ok, "Unreal found" if u_ok else "UnrealEditor.exe not found")
        self.map_hint.configure(text="→ /Game/GTAIV/Maps/%s" % self.map_name.get().strip()
                                if self._map_name_ok() else "letters, digits and _ only")
        b = self.blender.get()
        b_ok = os.path.isfile(b) and b.lower().endswith(".exe")
        self._set_status(self.blender_status, b_ok, "Blender found" if b_ok else "blender.exe not found")
        ok2 = (p_ok and u_ok and self._map_name_ok()) if self.target.get() == "UNREAL" else b_ok
        self._update_summary()
        return g_ok, ok2

    def _update_game_card(self, ok):
        gd = self.game_dir.get()
        if not ok:
            self.game_name.configure(text="Grand Theft Auto IV")
            self.game_info.configure(text="Not found: pick the folder in Settings", fg=BAD)
            return
        low = gd.lower()
        store = "Steam" if "steamapps" in low else ("Rockstar" if "rockstar" in low else "PC")
        eps = [n for n, d in (("TLAD", "TLAD"), ("TBoGT", "TBoGT")) if os.path.isdir(os.path.join(gd, d))]
        self.game_name.configure(text="GTA IV (%s)" % store)
        self.game_info.configure(text="GTAIV.exe" + (" · " + " + ".join(eps) if eps else ""), fg=FG2)

    def _unreal_cmd(self):
        return os.path.join(os.path.dirname(self.unreal.get()), "UnrealEditor-Cmd.exe")

    def _out_dir(self):
        if self.target.get() == "UNREAL":
            return os.path.dirname(self.uproject.get())
        return os.path.dirname(self.output.get())

    def _browse_game(self):
        d = filedialog.askdirectory(title="Folder that contains GTAIV.exe", initialdir=self.game_dir.get() or None)
        if d:
            self.game_dir.set(os.path.normpath(d))
            self.scan()

    def _browse_blender(self):
        f = filedialog.askopenfilename(title="Find blender.exe", filetypes=[("Blender", "blender.exe"), ("Programs", "*.exe")])
        if f:
            self.blender.set(os.path.normpath(f))

    def _browse_output(self):
        cur = self.output.get()
        f = filedialog.asksaveasfilename(title="Save Blender file as", defaultextension=".blend",
                                         filetypes=[("Blender file", "*.blend")],
                                         initialdir=os.path.dirname(cur) if cur else None,
                                         initialfile=os.path.basename(cur) if cur else "liberty_city.blend")
        if f:
            self.output.set(os.path.normpath(f))

    def _browse_uproject(self):
        cur = self.uproject.get()
        f = filedialog.askopenfilename(title="Unreal project to import into",
                                       filetypes=[("Unreal project", "*.uproject")],
                                       initialdir=os.path.dirname(cur) if cur else None)
        if f:
            self.uproject.set(os.path.normpath(f))
            ue = find_unreal(f)
            if ue:
                self.unreal.set(ue)

    def _browse_unreal(self):
        f = filedialog.askopenfilename(title="Find UnrealEditor.exe (Engine\\Binaries\\Win64)",
                                       filetypes=[("Unreal Editor", "UnrealEditor.exe"), ("Programs", "*.exe")])
        if f:
            self.unreal.set(os.path.normpath(f))

    # ------------------------------------------------------------ scanning
    def scan(self):
        gd = self.game_dir.get()
        if not is_game_dir(gd) or self._running():
            return
        self.rescan_btn.state(["disabled"])
        self.dist_sub.configure(text="Reading game files… (about half a minute)")
        self.status.configure(text="Reading game files…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

        def work():
            try:
                from gtaiv_exporter import mapdata
                data = mapdata.GameData(gd, log=lambda m: self.q.put(("log", m)),
                                        key_cache=os.path.join(SETTINGS_DIR, "gta4_key.bin"))
                data.load_definitions()
                data.open_archives()
                areas = data.load_instances()
                counts = {}  # area -> {"n": placements, "models": distinct models, "lods": LOD placements}
                for a, insts in areas.items():
                    hd = [i for i in insts if not i.is_lod and i.hash in data.models]
                    lod = sum(1 for i in insts if i.is_lod and i.hash in data.models)
                    if hd or lod:
                        counts[a] = {"n": len(hd), "models": len({i.hash for i in hd}), "lods": lod}
                for _, arc in data.archives:
                    arc.close()
                self.q.put(("scanned", counts))
            except Exception as e:
                self.q.put(("scan_failed", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _count(self, area, key="n"):
        return self.area_counts.get(area, {}).get(key, 0)

    def _populate_areas(self, keep_selection=False):
        selected = {a for a, v in self.area_vars.items() if v.get()} if keep_selection else None
        for w in self.table.inner.winfo_children():
            w.destroy()
        self.area_vars.clear()
        self.rows.clear()
        self.groups = {}
        if selected is None:
            selected = self._saved_areas & set(self.area_counts) or set(self.area_counts)
        grouped = {}
        for a in sorted(self.area_counts, key=natural_key):
            grouped.setdefault(group_of(a), []).append(a)
        for gname, _ in GROUPS:
            areas = grouped.get(gname, [])
            if not areas:
                continue
            head = tk.Frame(self.table.inner, bg=CARD)
            title = self._label(head, gname, font=self.f.section)
            title.pack(side="left", pady=(16, 8))
            info = self._label(head, "", font=self.f.small, fg=FG2)
            info.pack(side="left", padx=(10, 0), pady=(19, 8))
            Link(head, "None", lambda g=gname: self._select_group(g, False), self.f).pack(side="right", pady=(18, 8))
            tk.Label(head, text="·", fg=FG3, bg=CARD).pack(side="right", padx=4, pady=(18, 8))
            Link(head, "Select all", lambda g=gname: self._select_group(g, True), self.f).pack(side="right",
                                                                                              pady=(18, 8))
            line = tk.Frame(self.table.inner, bg=LINE, height=1)
            self.groups[gname] = (head, info, line, areas)
            for a in areas:
                v = tk.BooleanVar(value=a in selected)
                self.area_vars[a] = v
                self.rows[a] = self._make_row(a, gname, v)
                v.trace_add("write", lambda *x, a=a: self._area_changed(a))
        self._filter_rows()
        self._area_changed()

    def _make_row(self, area, gname, var):
        row = tk.Frame(self.table.inner, bg=CARD, cursor="hand2")
        self._grid_cols(row)
        tag, color = GROUP_STYLE.get(gname, GROUP_STYLE["Other"])
        tile = Tile(row, tag, color, self.f)
        tile.grid(row=0, column=0, rowspan=2, sticky="w", padx=(6, 0), pady=8)
        name = self._label(row, area.upper(), font=self.f.strong)
        name.grid(row=0, column=1, sticky="sw", padx=(8, 0), pady=(8, 0))
        sub = self._label(row, "%s · %s.wpl" % (gname, area), font=self.f.small, fg=FG2)
        sub.grid(row=1, column=1, sticky="nw", padx=(8, 0), pady=(0, 8))
        nums = []
        for col in (2, 3, 4):
            lab = self._label(row, "", fg=FG2)
            lab.grid(row=0, column=col, rowspan=2, sticky="w")
            nums.append(lab)
        st = tk.Frame(row, bg=CARD)
        st.grid(row=0, column=5, rowspan=2, sticky="w")
        dot = tk.Label(st, text="●", font=self.f.small, bg=CARD)
        dot.grid(row=0, column=0, sticky="w", padx=(0, 8))
        state = self._label(st, "", font=self.f.strong)
        state.grid(row=0, column=1, sticky="w")
        btn = ttk.Button(row, text="Add", width=9, style="Card.TButton", command=lambda: var.set(not var.get()))
        btn.grid(row=0, column=6, rowspan=2, sticky="e", padx=(0, 6))
        plain = [row, name, sub, st, dot, state] + nums

        def hover(on):
            for w in plain:
                w.configure(bg=HOVER if on else CARD)
            tile.set_bg(HOVER if on else CARD)
            btn.configure(style="Hover.TButton" if on else "Card.TButton")

        def leave(_e):
            w = self.winfo_containing(*self.winfo_pointerxy())
            if not (w and str(w).startswith(str(row))):
                hover(False)
        for w in plain + [tile, btn]:
            w.bind("<Enter>", lambda e: hover(True), add="+")
            w.bind("<Leave>", leave, add="+")
        for w in plain + [tile]:
            w.bind("<Button-1>", lambda e: var.set(not var.get()))
        return {"frame": row, "nums": nums, "dot": dot, "state": state, "btn": btn, "group": gname}

    def _refresh_row(self, area):
        r = self.rows.get(area)
        if not r:
            return
        on = self.area_vars[area].get()
        r["nums"][0].configure(text="{:,}".format(self._count(area)))
        r["nums"][1].configure(text="{:,}".format(self._count(area, "models")))
        lods = self._count(area, "lods")
        r["nums"][2].configure(text="{:,}".format(lods) if lods else "—")
        r["dot"].configure(fg=ACCENT if on else FG3)
        r["state"].configure(text="Selected" if on else "Not selected", fg=FG if on else FG2)
        r["btn"].configure(text="Remove" if on else "Add")

    def _group_info(self, areas, on):
        return "%s · %s placements · %d selected" % (
            plural(len(areas), "district"), "{:,}".format(sum(self._count(a) for a in areas)), on)

    def _filter_rows(self):
        if not self.rows:
            return
        q = self.search.get().strip().lower()
        for w in self.table.inner.winfo_children():
            w.pack_forget()
        for gname, (head, info, line, areas) in self.groups.items():
            shown = [a for a in areas if not q or q in a or q in gname.lower()]
            if not shown:
                continue
            info.configure(text=self._group_info(shown, sum(1 for a in shown if self.area_vars[a].get())))
            head.pack(fill="x", padx=4)
            line.pack(fill="x", pady=(0, 4))
            for a in shown:
                self.rows[a]["frame"].pack(fill="x", pady=1)
        self.table.to_top()

    def _select_group(self, g, value):
        for a in self.groups[g][3]:
            self.area_vars[a].set(value)

    def _select_all(self, value):
        for v in self.area_vars.values():
            v.set(value)

    def _area_changed(self, area=None):
        for a in ([area] if area else self.rows):
            self._refresh_row(a)
        for gname, (head, info, line, areas) in getattr(self, "groups", {}).items():
            info.configure(text=self._group_info(areas, sum(1 for a in areas if self.area_vars[a].get())))
        self._update_summary()

    def _selected(self):
        return [a for a, v in self.area_vars.items() if v.get()]

    def _estimate(self):
        n = sum(self._count(a) for a in self._selected())
        frac = min(1.0, n / FULL_MAP_PLACEMENTS)
        size = FULL_MAP_BYTES[self.target.get()] * (frac ** 0.8)  # textures are shared: grows sub-linearly
        size *= 0.3 + 0.7 * DETAIL_SHARE.get(self.detail.get(), 1.0)
        if not self.textures.get():
            size *= 0.3
        return n, max(size, 50e6) if n else 0

    def _free_space(self, path):
        probe = path or "."
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            return None

    def _extras_on(self):
        ue = self.target.get() == "UNREAL"
        return [label for key, label, blender in EXTRAS if (ue or blender) and getattr(self, key).get()]

    def _update_summary(self):
        if not hasattr(self, "card_disk") or not hasattr(self, "summary_vals"):
            return
        ue = self.target.get() == "UNREAL"
        sel = self._selected()
        n, size = self._estimate()
        free = self._free_space(self._out_dir())
        total = sum(self._count(a) for a in self.area_counts)
        if self.area_counts:
            self.dist_sub.configure(text="%d districts in %d boroughs · %s of %s placements selected" % (
                len(self.area_counts), len({group_of(a) for a in self.area_counts}), "{:,}".format(n),
                "{:,}".format(total)))
            self.all_btn.configure(text="%s  Select all (%d)" % (ICONS["add"], len(self.area_counts)))
        for b in (self.export_btn, self.export_btn2):
            if not self._running():
                b.state(["!disabled"] if n else ["disabled"])

        # disk card
        d = self.card_disk
        d["big"].configure(text="≈ %s" % fmt_bytes(size) if n else "—")
        if free is None:
            d["unit"].configure(text="folder not reachable", fg=BAD)
            ratio = 0
        else:
            d["unit"].configure(text="of %s free" % fmt_bytes(free), fg=BAD if size and free < size * 1.2 else FG2)
            ratio = min(1.0, size / free) if free else 1.0
        d["note"].configure(text=elide(self._out_dir() or "—", 46))
        bar = d["bar"]
        bar.update_idletasks()
        w = max(1, bar.winfo_width())
        bar.delete("all")
        bar.create_line(0, 1, w, 1, fill="#4a4a4a", width=1)
        bar.create_line(0, 1, max(3, int(w * ratio)), 1, fill=BAD if ratio > 0.8 else ACCENT, width=3)
        label = self.ue_space_status if ue else self.output_status
        if free is None:
            self._set_status(label, False, "folder not reachable")
        elif size and free < size * 1.2:
            self._set_status(label, False, "only %s free" % fmt_bytes(free))
        else:
            self._set_status(label, True, "%s free" % fmt_bytes(free))

        # target card
        t = self.card_target
        if ue:
            t["big"].configure(text="Unreal Engine")
            proj = os.path.splitext(os.path.basename(self.uproject.get()))[0] or "no project yet"
            t["unit"].configure(text="")
            t["note"].configure(text="Level %s in %s · %s" % (self.map_name.get().strip(), proj,
                                                               "instanced" if self.ue_mode.get() == "INSTANCES"
                                                               else "separate actors"))
        else:
            t["big"].configure(text="Blender")
            t["unit"].configure(text=".blend")
            t["note"].configure(text="%s · %s" % (os.path.basename(self.output.get()) or "—",
                                                   "Geometry Nodes instances" if self.mode.get() == "INSTANCES"
                                                   else "separate objects"))
        # detail card
        dname, ddesc = self._detail_parts()
        share = DETAIL_SHARE.get(self.detail.get(), 1.0)
        self.card_detail["big"].configure(text=dname)
        self.card_detail["unit"].configure(text="" if share == 1.0 else "≈ %d%% triangles" % round(share * 100))
        self.card_detail["note"].configure(text=ddesc[:1].upper() + ddesc[1:])
        # world card
        on = self._extras_on()
        avail = [label for key, label, blender in EXTRAS if ue or blender]
        x = self.card_extras
        x["big"].configure(text="%d of %d" % (len(on), len(avail)))
        x["unit"].configure(text="extras")
        x["note"].configure(text=", ".join(on) if on else "Untextured buildings and streets only")

        # export page
        self.exp_sub.configure(text="%s · %s · %s placements" % (
            "Unreal Engine" if ue else "Blender", plural(len(sel), "district"), "{:,}".format(n)))
        sv = self.summary_vals
        sv["Target"].configure(text="Unreal Engine" if ue else "Blender")
        names = ", ".join(sorted(sel, key=natural_key))
        sv["Districts"].configure(text=("%d: %s" % (len(sel), names if len(names) <= 34 else names[:33] + "…"))
                                  if sel else "none")
        sv["Placements"].configure(text="{:,}".format(n))
        sv["Detail"].configure(text=dname)
        world = ", ".join(on) or "none"
        sv["World"].configure(text=world if len(world) <= 40 else world[:39] + "…")
        sv["Output"].configure(text=elide(("/Game/GTAIV/Maps/" + self.map_name.get().strip()) if ue
                                          else self.output.get(), 40))

    # ------------------------------------------------------------ export
    def _running(self):
        return self.proc is not None or getattr(self, "_worker_busy", False)

    def export(self):
        g_ok, t_ok = self._check_paths()
        ue = self.target.get() == "UNREAL"
        if not g_ok:
            messagebox.showerror(APP_NAME, "Pick the GTA IV folder (the one that contains GTAIV.exe).")
            return
        if not t_ok:
            messagebox.showerror(APP_NAME, "Pick an Unreal project (.uproject), the Unreal Editor and a valid level name."
                                 if ue else "Blender wasn't found. Install Blender 4.2 or newer, or point to blender.exe.")
            return
        areas = self._selected()
        if not areas:
            messagebox.showerror(APP_NAME, "Select at least one district.")
            return
        n, size = self._estimate()
        out_dir = self._out_dir()
        if not ue:
            out = self.output.get().strip()
            if not out.lower().endswith(".blend"):
                out += ".blend"
                self.output.set(out)
            out_dir = os.path.dirname(out)
        try:
            os.makedirs(out_dir, exist_ok=True)
            free = shutil.disk_usage(out_dir).free
        except OSError as e:
            messagebox.showerror(APP_NAME, "Can't use that folder:\n%s" % e)
            return
        if free < size * 1.2 and not messagebox.askyesno(
                APP_NAME, "This export needs about %s but only %s is free on that drive.\n\nContinue anyway?"
                % (fmt_bytes(size), fmt_bytes(free))):
            return
        if ue:
            if unreal_running() and not messagebox.askyesno(
                    APP_NAME, "Unreal Editor is running.\n\nClose it first if it has this project open, "
                              "otherwise the import can't save its files.\n\nContinue anyway?"):
                return
            mp = os.path.join(out_dir, "Content", "GTAIV", "Maps", self.map_name.get().strip() + ".umap")
            if os.path.exists(mp) and not messagebox.askyesno(
                    APP_NAME, "The level %s already exists in this project. Replace its contents?"
                    % self.map_name.get().strip()):
                return
        elif os.path.exists(out) and not messagebox.askyesno(APP_NAME, "%s already exists. Replace it?" % out):
            return

        self._persist()
        self._result = None
        self.pct.configure(fg=FG)
        self.cancelled = False
        self.start_time = time.time()
        self._busy(True)
        self._show("export")
        self.pct.configure(text="0%")
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._log("Exporting %d districts (%s placements) to %s"
                  % (len(areas), "{:,}".format(n), "Unreal" if ue else out))
        if ue:
            self._export_unreal(areas)
        else:
            self._export_blender(areas, out)

    # Blender: one Blender process does everything
    def _export_blender(self, areas, out):
        out_dir = os.path.dirname(out)
        stem = os.path.splitext(os.path.basename(out))[0]
        tmp = os.path.join(out_dir, ".gta4_tmp")
        os.makedirs(tmp, exist_ok=True)
        job = {
            "addon_dir": HERE,
            "game_dir": self.game_dir.get(),
            "cache_dir": os.path.join(out_dir, stem + "_textures"),
            "output": out,
            "areas": ",".join(areas),
            "lods": False,
            "detail": self.detail.get(),
            "interiors": self.interiors.get(),
            "lights": self.lights.get(),
            "mode": self.mode.get(),
            "textures": self.textures.get(),
            "normals": self.normals.get(),
            "key_cache": os.path.join(SETTINGS_DIR, "gta4_key.bin"),
        }
        job_path = os.path.join(tmp, "job.json")
        with open(job_path, "w", encoding="utf-8") as f:
            json.dump(job, f)
        env = dict(os.environ, TEMP=tmp, TMP=tmp)
        cmd = [self.blender.get(), "-b", "--factory-startup", "--python",
               os.path.join(HERE, "blender_runner.py"), "--", job_path]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                         text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        except OSError as e:
            self._busy(False)
            messagebox.showerror(APP_NAME, "Couldn't start Blender:\n%s" % e)
            return
        self.status.configure(text="Starting Blender…")

        def read():
            for line in self.proc.stdout:
                self._handle_line(line.rstrip(), 0.0, 1.0)
            code = self.proc.wait()
            shutil.rmtree(tmp, ignore_errors=True)
            self.q.put(("exit", code))
        threading.Thread(target=read, daemon=True).start()

    # Unreal: export files here (no Blender needed), then a headless editor imports them
    def _export_unreal(self, areas):
        project_dir = os.path.dirname(self.uproject.get())
        export_dir = os.path.join(project_dir, "Saved", "GTAIV_Export")
        self._worker_busy = True
        self.status.configure(text="Reading game archives…")

        def work():
            try:
                self._convert_for_unreal(areas, export_dir)
                if self.cancelled:
                    raise RuntimeError("cancelled")
                if self.traffic.get() or self.characters.get():  # the plugin also runs the pedestrians
                    install_traffic_plugin(self.uproject.get(), lambda m: self.q.put(("log", m)))
                self._run_unreal_passes(project_dir, export_dir)
                if not self.keep_files.get() and isinstance(self._result, int):
                    shutil.rmtree(export_dir, ignore_errors=True)
                self.q.put(("exit", 0))
            except Exception as e:
                if not self.cancelled:
                    self.q.put(("failed", str(e)))
                self.q.put(("exit", -1))
            finally:
                self._worker_busy = False
        threading.Thread(target=work, daemon=True).start()

    def _convert_for_unreal(self, areas, export_dir):
        """Step 1 (no Unreal needed): game files -> glTF + PNG + JSON. Reused when a previous
        run with the same selection didn't finish, so a retry goes straight to importing."""
        from gtaiv_exporter.unreal_export import UnrealExporter
        sig = {"areas": sorted(areas), "detail": self.detail.get(), "interiors": self.interiors.get(),
               "lights": self.lights.get(), "traffic": self.traffic.get(), "characters": self.characters.get(),
               "textures": self.textures.get(), "v": 7}
        sig_path = os.path.join(export_dir, "selection.json")
        try:
            if (json.load(open(sig_path, encoding="utf-8")) == sig
                    and os.path.isfile(os.path.join(export_dir, "gta4_unreal.json"))):
                self.q.put(("log", "Reusing converted files from the previous unfinished run"))
                return
        except Exception:
            pass
        shutil.rmtree(export_dir, ignore_errors=True)
        os.makedirs(export_dir, exist_ok=True)
        n = UnrealExporter(self.game_dir.get(), export_dir, ",".join(areas), False,
                           self.textures.get(), key_cache=os.path.join(SETTINGS_DIR, "gta4_key.bin"),
                           log=lambda m: self.q.put(("log", m)), detail=self.detail.get(),
                           interiors=self.interiors.get(), lights=self.lights.get(),
                           traffic=self.traffic.get(), characters=self.characters.get()).run(
            progress=lambda f: self.q.put(("progress", (UE_CONVERT_SHARE * f, "Converting models"))))
        with open(sig_path, "w", encoding="utf-8") as f:
            json.dump(sig, f)
        self.q.put(("log", "Converted %s placements" % "{:,}".format(n)))

    def _run_unreal_passes(self, project_dir, export_dir):
        """Step 2: import with headless Unreal, a few mesh batches per editor process.

        A single long-running editor runs out of memory on big exports (its memory
        only grows while importing). Each pass resumes where the previous one
        stopped, and a crashed pass is retried with smaller chunks."""
        job_path = os.path.join(export_dir, "job.json")
        chunk = UE_BATCHES_PER_PASS
        failures = 0
        pass_no = 0
        first = True
        while not self.cancelled:
            pass_no += 1
            job = {"export_dir": export_dir, "mode": self.ue_mode.get(), "nanite": self.nanite.get(),
                   "collision": self.collision.get(), "map_name": self.map_name.get().strip(),
                   "stage": "all", "first_pass": first, "max_batches": chunk, "lights": self.lights.get()}
            with open(job_path, "w", encoding="utf-8") as f:
                json.dump(job, f)
            log_path = os.path.join(export_dir, "unreal_pass_%02d.log" % pass_no)
            self._pass = {"more": None, "failed": None}
            self.q.put(("log", "Unreal pass %d (up to %d mesh batches)…" % (pass_no, chunk)))
            if pass_no == 1:
                self.q.put(("progress", (UE_CONVERT_SHARE, "Starting Unreal Editor")))
            started = time.time()
            code = self._run_unreal_once(project_dir, job_path, log_path)
            if self.cancelled:
                return
            if isinstance(self._result, int):
                return  # level built
            if self._pass["failed"]:
                raise RuntimeError(self._pass["failed"])  # a script error: retrying won't help
            first = False
            if self._pass["more"] is not None:
                failures = 0
                chunk = min(UE_BATCHES_PER_PASS, chunk * 2)  # recover after a crash shrank it
                continue
            # the editor died without reporting back: a crash, most often out of memory
            reason = self._crash_reason(project_dir, started) or "Unreal exited unexpectedly (code %s)" % code
            failures += 1
            self.q.put(("log", "Unreal stopped: %s" % reason))
            if failures >= UE_MAX_RETRIES:
                raise RuntimeError("%s.\n\nGave up after %d attempts in a row. Meshes imported so far are "
                                   "kept, so exporting again resumes where it stopped." % (reason, failures))
            chunk = max(1, chunk // 2)
            self.q.put(("log", "Retrying from where it stopped (attempt %d of %d)…"
                        % (failures + 1, UE_MAX_RETRIES)))

    def _run_unreal_once(self, project_dir, job_path, log_path):
        cmd = ('"%s" "%s" -run=pythonscript -script="%s" -unattended -nop4 -nosplash -NoSound '
               '-EnablePlugins=PythonScriptPlugin,EditorScriptingUtilities -abslog="%s"'
               % (self._unreal_cmd(), self.uproject.get(), os.path.join(HERE, "unreal_runner.py"), log_path))
        env = dict(os.environ, GTA4_UE_JOB=job_path)
        # keep Unreal's derived-data cache with the project (the system drive may be full)
        env["UE-LocalDataCachePath"] = os.path.join(project_dir, "DerivedDataCache")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     env=env, creationflags=NO_WINDOW)
        self._tail_log(log_path, self.proc)
        code = self.proc.wait()
        time.sleep(0.5)
        self._tail_log(log_path, None)
        self.proc = None
        return code

    @staticmethod
    def _crash_reason(project_dir, since):
        crashes = os.path.join(project_dir, "Saved", "Crashes")
        try:
            dirs = [os.path.join(crashes, d) for d in os.listdir(crashes)]
            dirs = [d for d in dirs if os.path.getmtime(d) >= since - 5]
        except OSError:
            return None
        if not dirs:
            return None
        try:
            text = open(os.path.join(max(dirs, key=os.path.getmtime), "CrashContext.runtime-xml"),
                        encoding="utf-8", errors="replace").read()
        except OSError:
            return "Unreal crashed"
        ctype = re.search(r"<CrashType>(.*?)</CrashType>", text)
        if ctype and "OutOfMemory" in ctype.group(1):
            return "Unreal ran out of memory"
        msg = re.search(r"<ErrorMessage>(.*?)</ErrorMessage>", text, re.S)
        return "Unreal crashed: " + (msg.group(1).strip()[:200] if msg else (ctype.group(1) if ctype else "unknown"))

    def _tail_log(self, path, proc):
        pos = getattr(self, "_tail_pos", 0) if proc is None else 0
        buf = ""
        while True:
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                buf += chunk
                *lines, buf = buf.split("\n")
                for line in lines:
                    m = re.search(r"LogPython: (?:Display: )?(@@.*)$", line)
                    if m:
                        self._handle_line(m.group(1).rstrip(), UE_CONVERT_SHARE, 1.0)
                    elif "LogPython: Error" in line:
                        self.q.put(("log", line.split("LogPython: Error: ", 1)[-1].rstrip()))
            self._tail_pos = pos
            if proc is None or proc.poll() is not None:
                return
            time.sleep(0.5)

    def _handle_line(self, line, p0, p1):
        if line.startswith("@@PROGRESS"):
            f = float(line.split()[1])
            phase = ("Importing into Unreal" if p0 > 0 else
                     ("Building models" if f < 0.8 else "Placing objects"))
            self.q.put(("progress", (p0 + (p1 - p0) * f, phase)))
        elif line.startswith("@@SAVING"):
            self.q.put(("phase", "Saving .blend file…"))
        elif line.startswith("@@DONE"):
            self._result = int(line.split()[1])
        elif line.startswith("@@FAILED"):
            if p0 > 0:
                self._pass["failed"] = line[9:]  # the Unreal pass loop reports it
            else:
                self.q.put(("failed", line[9:]))
        elif line.startswith("@@MORE"):
            self._pass["more"] = int(line.split()[1])
        elif line.startswith("@@LOG"):
            self.q.put(("log", line[6:]))
        elif line.startswith("[GTA IV]") or "Error" in line or "Traceback" in line or line.startswith("  File"):
            self.q.put(("log", line.replace("[GTA IV] ", "")))

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self._log("Cancelling…")

    def _busy(self, busy):
        st = ["disabled"] if busy else ["!disabled"]
        for b in (self.export_btn, self.export_btn2, self.rescan_btn):
            b.state(st)
        self.cancel_btn.state(["!disabled"] if busy else ["disabled"])
        if busy:
            self.open_btn.state(["disabled"])
        else:
            self._update_summary()

    def _persist(self):
        save_settings({
            "target": self.target.get(), "game_dir": self.game_dir.get(), "blender": self.blender.get(),
            "output": self.output.get(), "uproject": self.uproject.get(), "unreal": self.unreal.get(),
            "map_name": self.map_name.get(), "mode": self.mode.get(), "ue_mode": self.ue_mode.get(),
            "detail": self.detail.get(), "interiors": self.interiors.get(), "lights": self.lights.get(),
            "traffic": self.traffic.get(), "characters": self.characters.get(),
            "textures": self.textures.get(), "normals": self.normals.get(),
            "nanite": self.nanite.get(), "collision": self.collision.get(), "keep_files": self.keep_files.get(),
            "areas": self._selected(),
        })

    def open_result(self):
        if self.target.get() == "UNREAL":
            if unreal_running():
                messagebox.showinfo(APP_NAME, "Unreal Editor is already running. Open the level "
                                    "Content/GTAIV/Maps/%s from the Content Browser." % self.map_name.get().strip())
                return
            subprocess.Popen([self.unreal.get(), self.uproject.get(),
                              "/Game/GTAIV/Maps/%s" % self.map_name.get().strip()])
        else:
            out = self.output.get()
            if os.path.isfile(out):
                subprocess.Popen([self.blender.get(), out])

    def open_folder(self):
        d = self._out_dir()
        if self.target.get() == "UNREAL":
            c = os.path.join(d, "Content", "GTAIV")
            d = c if os.path.isdir(c) else d
        if os.path.isdir(d):
            os.startfile(d)
        else:
            messagebox.showinfo(APP_NAME, "The folder doesn't exist yet; it is created when you export.")

    def _on_close(self):
        if self._running():
            if not messagebox.askyesno(APP_NAME, "An export is running. Stop it and quit?"):
                return
            self.cancel()
        self._persist()
        self.destroy()

    # ------------------------------------------------------------ events
    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self._log(val)
                    if self._running() and self.progress["value"] == 0:
                        self.status.configure(text="Reading game archives… (about half a minute)")
                elif kind == "scanned":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self.rescan_btn.state(["!disabled"])
                    self.area_counts = val
                    self._populate_areas(keep_selection=bool(self.area_vars))
                    self.status.configure(text="Found %d districts. Pick what to export." % len(val))
                elif kind == "scan_failed":
                    self.progress.stop()
                    self.rescan_btn.state(["!disabled"])
                    self.dist_sub.configure(text="Couldn't read the game files")
                    self.status.configure(text="Couldn't read the game files.")
                    self._log("Error: " + val)
                    messagebox.showerror(APP_NAME, "Couldn't read the game files:\n%s" % val)
                elif kind == "progress":
                    f, phase = val
                    self.progress.configure(value=f * 1000)
                    self.pct.configure(text="%d%%" % (f * 100))
                    el = time.time() - self.start_time
                    eta = " · about %s left" % self._dur(el / f - el) if f > 0.05 else ""
                    self.status.configure(text="%s… %d%%%s" % (phase, f * 100, eta))
                elif kind == "phase":
                    self.status.configure(text=val)
                elif kind == "failed":
                    self._log("Error: " + val)
                    self._result = ("failed", val)
                elif kind == "exit":
                    self.proc = None
                    self._worker_busy = False
                    self._busy(False)
                    self._finish(val)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _finish(self, code):
        ue = self.target.get() == "UNREAL"
        if isinstance(self._result, int):
            self.progress.configure(value=1000)
            self.pct.configure(text="Done")
            where = ("level /Game/GTAIV/Maps/%s in %s" % (self.map_name.get().strip(),
                                                         os.path.basename(self.uproject.get()))
                     if ue else self.output.get())
            msg = "Done in %s: %s placements saved to %s" % (
                self._dur(time.time() - self.start_time), "{:,}".format(self._result), where)
            self.status.configure(text=msg)
            self._log(msg)
            self.open_btn.state(["!disabled"])
        elif self.cancelled:
            self.status.configure(text="Cancelled.")
            self.pct.configure(text="Cancelled")
            self.progress.configure(value=0)
        elif isinstance(self._result, tuple):
            self.status.configure(text="Export failed: " + self._result[1])
            self.pct.configure(text="Failed", fg=BAD)
            messagebox.showerror(APP_NAME, "Export failed:\n%s\n\nSee the log for details." % self._result[1])
        else:
            self.status.configure(text="Export stopped (exit code %s)." % code)
            self.progress.configure(value=0)
        if ue and not isinstance(self._result, int) and os.path.isfile(self._map_file()):
            self.open_btn.state(["!disabled"])  # a level from an earlier run can still be opened

    def _map_file(self):
        return os.path.join(os.path.dirname(self.uproject.get()), "Content", "GTAIV", "Maps",
                            self.map_name.get().strip() + ".umap")

    @staticmethod
    def _dur(s):
        s = int(max(0, s))
        return "%dm %02ds" % (s // 60, s % 60) if s >= 60 else "%ds" % s



def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    main()
