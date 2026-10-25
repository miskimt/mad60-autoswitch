#!/usr/bin/env python3
"""
MAD60 HE Auto Profile Switcher — GUI Edition

A standalone Windows GUI application that:
 • Scans the FGG Hub (https://hub.fgg.com.cn/) IndexedDB for keyboard profiles
 • Watches for running game processes
 • Tells the browser extension which profile to select in the MadLions web driver
 • Provides a system-tray icon and modern dark-themed settings window

Build with:
    pyinstaller --onefile --noconsole --icon=mad60_icon.ico --name MAD60AutoSwitch mad60_autoswitch_gui.py
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import json
import logging
import os
import re
import struct
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import tkinter.messagebox as tkmb
import tkinter.ttk as ttk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_PATH = os.path.join(HERE, "kb_autoswitch.log")
ICON_PATH = os.path.join(HERE, "mad60_icon.ico")
LONG_POLL_SECONDS = 25
DRIVER_GONE_AFTER = 60

log = logging.getLogger("kb")

DEFAULTS = {
    "port": 47831,
    "poll_seconds": 2,
    "exit_grace_seconds": 15,
    "default_profile": "normal",
    "apply_button_text": "",
    "games": [],
}


# =========================================================================
#  Profile Scanner — reads profile names from FGG Hub's IndexedDB on disk
# =========================================================================
def _find_indexeddb_dirs() -> list[str]:
    """Locate all IndexedDB folders for hub.fgg.com.cn across Chrome/Edge/Brave."""
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return []
    candidates = [
        os.path.join(local, "Google", "Chrome", "User Data"),
        os.path.join(local, "Microsoft", "Edge", "User Data"),
        os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data"),
        os.path.join(local, "Google", "Chrome SxS", "User Data"),  # Chrome Canary
    ]
    results = []
    for browser_data in candidates:
        if not os.path.isdir(browser_data):
            continue
        # check Default and Profile N
        profiles = ["Default"] + [
            d for d in os.listdir(browser_data)
            if re.match(r"Profile \d+", d) and os.path.isdir(os.path.join(browser_data, d))
        ]
        for prof in profiles:
            idb = os.path.join(
                browser_data, prof, "IndexedDB",
                "https_hub.fgg.com.cn_0.indexeddb.leveldb",
            )
            if os.path.isdir(idb):
                results.append(idb)
    return results


def _extract_strings_from_ldb(path: str) -> set[str]:
    """Extract readable ASCII/UTF-8 strings from a LevelDB data file."""
    strings = set()
    try:
        with open(path, "rb") as f:
            data = f.read()
        # Find UTF-8 / ASCII printable strings ≥ 2 chars
        for m in re.finditer(rb"[\x20-\x7e]{2,80}", data):
            strings.add(m.group().decode("utf-8", "replace"))
    except (OSError, PermissionError):
        pass
    return strings


def _is_likely_profile_name(name: str) -> bool:
    """Heuristic: profile names are short, alphanumeric with underscores, no code-like patterns."""
    if len(name) < 2 or len(name) > 40:
        return False
    # skip things that look like code keywords, LevelDB internals, etc.
    skip_patterns = {
        "leveldb", "filter", "BuiltinBloomFilter", "MANIFEST", "CURRENT",
        "function", "return", "const", "class", "import", "export", "module",
        "undefined", "null", "true", "false", "prototype", "constructor",
        "script", "style", "document", "window", "console",
    }
    if name.lower() in skip_patterns:
        return False
    # Must look like an identifier (letters, numbers, underscore, space, dash)
    if not re.match(r"^[A-Za-z0-9_ -]+$", name):
        return False
    # Skip pure numbers or single chars
    if name.isdigit():
        return False
    return True


def scan_profiles_from_indexeddb() -> list[str]:
    """Scan all browser IndexedDB stores for FGG Hub profile names."""
    all_strings: set[str] = set()
    dirs = _find_indexeddb_dirs()
    for idb_dir in dirs:
        # Read .ldb files (can be read even while Chrome is running, unlike .log)
        for ldb_file in glob.glob(os.path.join(idb_dir, "*.ldb")):
            all_strings |= _extract_strings_from_ldb(ldb_file)

    # Extract JSON-ish "name" field values that look like profile names
    # The IndexedDB stores profile objects with a "name" field
    profile_candidates: set[str] = set()

    # Strategy 1: look for known field pattern near "name" keyword
    for s in all_strings:
        # Look for strings that appear as values near "name" keys in binary data
        stripped = s.strip()
        if _is_likely_profile_name(stripped):
            profile_candidates.add(stripped)

    # Strategy 2: also read from the config.json itself to get any already-known profiles
    known_profiles = set()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        for g in cfg.get("games", []):
            if g.get("profile"):
                known_profiles.add(g["profile"])
        dp = cfg.get("default_profile", "")
        if dp:
            known_profiles.add(dp)
    except (OSError, json.JSONDecodeError):
        pass

    # Filter: keep only candidates that are confirmed by being present in known_profiles
    # OR look like plausible profile names (short, no code junk)
    # We specifically look for strings that also appear as IndexedDB values
    # near key-like patterns
    confirmed = set()
    for s in profile_candidates:
        # If it's in known_profiles, definitely include
        if s in known_profiles:
            confirmed.add(s)
            continue
        # If it looks like a short, clean name and doesn't overlap with LevelDB internals
        if (
            len(s) >= 2
            and len(s) <= 30
            and not any(c in s for c in "{}[]();=<>")
            and not s.startswith("http")
            and not s.endswith(".js")
            and not s.endswith(".css")
            and s.lower() not in {
                "adv", "advance", "apc", "col", "dks", "mac", "press",
                "release", "name", "data", "mode", "feature", "speed",
                "row", "filter", "color", "brightness", "effect",
                "top", "level", "key", "type", "info", "list",
                "area", "lock", "swap", "dead", "band", "mix",
                "tap", "win", "rgb", "nkro", "wasd", "protocol",
                "version", "custom", "action", "bottom", "optimize",
                "switch", "light", "game", "keyboard",
            }
            and "_" in s or s.islower() or s[0].isupper()
        ):
            # Heuristic: profile names commonly use underscores or are lowercase words
            if "_" in s or (s.isalpha() and len(s) >= 3):
                confirmed.add(s)

    # Also always include known config profiles
    confirmed |= known_profiles

    return sorted(confirmed)


# =========================================================================
#  Config
# =========================================================================
def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    games = []
    for g in cfg["games"]:
        procs = [p.strip().lower() for p in g.get("processes", []) if p.strip()]
        if not g.get("profile") or not procs:
            raise ValueError("every game needs a 'profile' and at least one process: %r" % (g,))
        games.append({
            "name": g.get("name") or g["profile"],
            "profile": g["profile"],
            "processes": procs,
        })
    cfg["games"] = games
    return cfg


def save_config(cfg: dict, path: str = CONFIG_PATH):
    """Save current config back to config.json."""
    out = {
        "port": cfg["port"],
        "poll_seconds": cfg["poll_seconds"],
        "exit_grace_seconds": cfg["exit_grace_seconds"],
        "default_profile": cfg["default_profile"],
        "apply_button_text": cfg["apply_button_text"],
        "games": [
            {
                "name": g["name"],
                "profile": g["profile"],
                "processes": g["processes"] if isinstance(g["processes"], list) else [g["processes"]],
            }
            for g in cfg["games"]
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")


# =========================================================================
#  Process listing (Windows)
# =========================================================================
def list_processes() -> set:
    from ctypes import POINTER, Structure, c_int32, c_size_t, c_uint32, c_void_p, c_wchar, sizeof

    class PROCESSENTRY32W(Structure):
        _fields_ = [
            ("dwSize", c_uint32), ("cntUsage", c_uint32), ("th32ProcessID", c_uint32),
            ("th32DefaultHeapID", c_size_t), ("th32ModuleID", c_uint32), ("cntThreads", c_uint32),
            ("th32ParentProcessID", c_uint32), ("pcPriClassBase", c_int32), ("dwFlags", c_uint32),
            ("szExeFile", c_wchar * 260),
        ]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.argtypes = [c_uint32, c_uint32]
    k32.CreateToolhelp32Snapshot.restype = c_void_p
    k32.Process32FirstW.argtypes = [c_void_p, POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [c_void_p, POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [c_void_p]

    TH32CS_SNAPPROCESS = 0x2
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, c_void_p(-1).value):
        raise OSError("CreateToolhelp32Snapshot failed")
    names = set()
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            names.add(entry.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return names


# =========================================================================
#  Watcher — decision logic
# =========================================================================
class Watcher:
    """Turns 'which processes are running' into 'which profile should be active'."""

    def __init__(self, cfg: dict):
        self.games = cfg["games"]
        self.default = cfg["default_profile"]
        self.grace = cfg["exit_grace_seconds"]
        self.first_seen: dict = {}
        self.idle_since = None
        self.target = None

    def tick(self, running: set, now: float):
        """Returns (profile, reason) when the target changes, else None."""
        active = []
        for g in self.games:
            if any(p in running for p in g["processes"]):
                self.first_seen.setdefault(g["name"], now)
                active.append(g)
            else:
                self.first_seen.pop(g["name"], None)

        if active:
            g = max(active, key=lambda x: self.first_seen[x["name"]])
            self.idle_since = None
            new, reason = g["profile"], "%s is running" % g["name"]
        else:
            if self.idle_since is None:
                self.idle_since = now
            if self.target not in (None, self.default) and now - self.idle_since < self.grace:
                return None
            new, reason = self.default, "no game running"

        if new != self.target:
            self.target = new
            return new, reason
        return None


# =========================================================================
#  Shared state + HTTP server (for the browser extension)
# =========================================================================
class State:
    def __init__(self, apply_button_text: str = ""):
        self.cond = threading.Condition()
        self.rev = 0
        self.desired = None
        self.reason = ""
        self.applied = None
        self.apply_button_text = apply_button_text
        self.last_report = None
        self.last_seen = 0.0
        self.started = time.time()
        self._last_logged_report = None
        # GUI observable callbacks
        self._on_change_callbacks: list = []

    def add_listener(self, fn):
        self._on_change_callbacks.append(fn)

    def _notify_listeners(self):
        for fn in self._on_change_callbacks:
            try:
                fn()
            except Exception:
                pass

    def set_desired(self, profile: str, reason: str):
        with self.cond:
            if profile == self.desired:
                return
            self.desired, self.reason = profile, reason
            self.rev += 1
            self.cond.notify_all()
        log.info("target profile -> %s (%s)", profile, reason)
        self._notify_listeners()

    def snapshot(self) -> dict:
        return {
            "rev": self.rev,
            "profile": self.desired,
            "pending": self.desired is not None and self.desired != self.applied,
            "applyButtonText": self.apply_button_text,
        }

    def report(self, payload: dict):
        profile = payload.get("profile")
        ok = bool(payload.get("ok"))
        detail = str(payload.get("detail", ""))
        with self.cond:
            if ok and profile == self.desired:
                self.applied = profile
            self.last_report = {"profile": profile, "ok": ok, "detail": detail, "at": time.time()}
            key = (profile, ok, detail)
            first_time = key != self._last_logged_report
            self._last_logged_report = key
        if first_time:
            (log.info if ok else log.warning)(
                "driver page: %s '%s' (%s)",
                "selected" if ok else "FAILED to select",
                profile,
                detail,
            )
        self._notify_listeners()


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
            return host in ("127.0.0.1", "localhost")

        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj, indent=2).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            url = urlparse(self.path)
            if url.path == "/next":
                try:
                    since = int(parse_qs(url.query).get("since", ["-1"])[0])
                except ValueError:
                    since = -1
                now = time.time()
                if now - state.last_seen > DRIVER_GONE_AFTER:
                    log.info("driver page connected")
                state.last_seen = now
                with state.cond:
                    if state.rev == since:
                        state.cond.wait_for(lambda: state.rev != since, timeout=LONG_POLL_SECONDS)
                    snap = state.snapshot()
                state.last_seen = time.time()
                return self._send(200, snap)
            if url.path == "/status":
                age = time.time() - state.last_seen if state.last_seen else None
                return self._send(200, {
                    "target_profile": state.desired,
                    "reason": state.reason,
                    "confirmed_in_driver": state.applied,
                    "driver_page_connected": age is not None and age < DRIVER_GONE_AFTER,
                    "seconds_since_driver_page_polled": None if age is None else round(age, 1),
                    "last_report": state.last_report,
                    "uptime_seconds": round(time.time() - state.started),
                })
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            if urlparse(self.path).path != "/report":
                return self._send(404, {"error": "not found"})
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 65536)
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (ValueError, OSError):
                return self._send(400, {"error": "bad json"})
            state.report(payload)
            return self._send(200, {"ok": True})

    return Handler


# =========================================================================
#  Logging
# =========================================================================
def setup_logging(console: bool = True) -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=200_000, backupCount=1, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if console and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


# =========================================================================
#  System Tray Icon (using ctypes Shell_NotifyIconW)
# =========================================================================
WM_USER = 0x0400
WM_TRAY = WM_USER + 20
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010

NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIM_ADD = 0x00
NIM_MODIFY = 0x01
NIM_DELETE = 0x02

WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_LEFTALIGN = 0x0000
TPM_RETURNCMD = 0x0100

IDI_APPLICATION = 32512


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint32),
        ("uFlags", ctypes.c_uint32),
        ("uCallbackMessage", ctypes.c_uint32),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
    ]


class SystemTray:
    """Minimal system-tray icon for Windows, no extra dependencies."""

    def __init__(self, tooltip: str, on_open, on_quit):
        self.on_open = on_open
        self.on_quit = on_quit
        self._tooltip = tooltip
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._hwnd = None
        self._nid = None

    def start(self):
        self._thread.start()

    def update_tooltip(self, text: str):
        self._tooltip = text[:127]
        if self._hwnd and self._nid:
            self._nid.szTip = self._tooltip
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    def _run(self):
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_long))

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        shell32 = ctypes.windll.shell32

        # Create a hidden window class
        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("style", ctypes.c_uint),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
                ("hIconSm", ctypes.c_void_p),
            ]

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                lp = lparam if isinstance(lparam, int) else (ctypes.cast(lparam, ctypes.POINTER(ctypes.c_long)).contents.value if lparam else 0)
                if lp == WM_RBUTTONUP:
                    self._show_menu()
                elif lp == WM_LBUTTONDBLCLK:
                    self.on_open()
                return 0
            elif msg == WM_COMMAND:
                wp = wparam if isinstance(wparam, int) else (ctypes.cast(wparam, ctypes.POINTER(ctypes.c_ulong)).contents.value if wparam else 0)
                if wp == 1:
                    self.on_open()
                elif wp == 2:
                    self.on_quit()
                return 0
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p
        )(wndproc)

        hinstance = kernel32.GetModuleHandleW(None)
        class_name = "MAD60TrayClass"

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
        wc.hInstance = hinstance
        wc.lpszClassName = class_name

        user32.RegisterClassExW(ctypes.byref(wc))

        self._hwnd = user32.CreateWindowExW(
            0, class_name, "MAD60AutoSwitch", 0,
            0, 0, 0, 0,
            None, None, hinstance, None,
        )

        # Load icon
        hicon = None
        if os.path.exists(ICON_PATH):
            hicon = user32.LoadImageW(
                None, ICON_PATH, 1, 0, 0, 0x00000010  # IMAGE_ICON, LR_LOADFROMFILE
            )
        if not hicon:
            hicon = user32.LoadIconW(None, IDI_APPLICATION)

        # Add tray icon
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = hicon
        nid.szTip = self._tooltip
        self._nid = nid

        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

        # Message loop
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # Cleanup
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))

    def _show_menu(self):
        user32 = ctypes.windll.user32
        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, MF_STRING, 1, "Open MAD60 AutoSwitch")
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, MF_STRING, 2, "Quit")

        pt = ctypes.wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self._hwnd)

        cmd = user32.TrackPopupMenu(
            hmenu, TPM_LEFTALIGN | TPM_RETURNCMD,
            pt.x, pt.y, 0, self._hwnd, None,
        )
        user32.DestroyMenu(hmenu)

        if cmd == 1:
            self.on_open()
        elif cmd == 2:
            self.on_quit()

    def destroy(self):
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)


# =========================================================================
#  GUI Application
# =========================================================================
# Color theme
COLORS = {
    "bg": "#1a1a2e",
    "bg_secondary": "#16213e",
    "bg_card": "#0f3460",
    "accent": "#e94560",
    "accent_hover": "#ff6b81",
    "text": "#eaeaea",
    "text_dim": "#a0a0b0",
    "text_bright": "#ffffff",
    "success": "#00d97e",
    "warning": "#f0ad4e",
    "border": "#2a2a4a",
    "input_bg": "#0d1b36",
    "button_bg": "#e94560",
    "button_hover": "#ff6b81",
    "scrollbar": "#2a2a4a",
}


class MAD60App:
    def __init__(self):
        self.cfg = None
        self.state = None
        self.server = None
        self.watcher = None
        self.running = True
        self.scanned_profiles: list[str] = []
        self.root = None
        self.tray = None

        # Load config
        setup_logging(console=not getattr(sys, "frozen", False))
        try:
            self.cfg = load_config()
        except (OSError, ValueError) as e:
            log.error("config.json problem: %s", e)
            self.cfg = dict(DEFAULTS)

        # Start state and server
        self.state = State(self.cfg.get("apply_button_text", ""))
        self.state.add_listener(self._on_state_change)

        # Initial profile scan
        self._scan_profiles()

        # Start HTTP server
        self._start_server()

        # Start watcher thread
        self._start_watcher()

        # Create GUI
        self._create_gui()

        # Create system tray
        self.tray = SystemTray(
            "MAD60 AutoSwitch",
            on_open=self._show_window,
            on_quit=self._quit,
        )
        self.tray.start()

        # Start GUI
        self.root.mainloop()

    def _scan_profiles(self):
        """Scan for profiles from browser IndexedDB."""
        try:
            self.scanned_profiles = scan_profiles_from_indexeddb()
            log.info("Scanned profiles: %s", ", ".join(self.scanned_profiles) or "(none found)")
        except Exception as e:
            log.warning("Profile scan error: %s", e)
            self.scanned_profiles = []

    def _start_server(self):
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", self.cfg["port"]), make_handler(self.state))
            self.server.daemon_threads = True
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            log.info("helper running on 127.0.0.1:%s", self.cfg["port"])
        except OSError as e:
            log.error("Could not open port %s (%s). Is another copy already running?", self.cfg["port"], e)

    def _start_watcher(self):
        self.watcher = Watcher(self.cfg)
        t = threading.Thread(target=self._watcher_loop, daemon=True)
        t.start()

    def _watcher_loop(self):
        for g in self.cfg["games"]:
            log.info("watching for %s -> profile '%s'", " / ".join(g["processes"]), g["profile"])

        while self.running:
            try:
                change = self.watcher.tick(list_processes(), time.time())
                if change:
                    self.state.set_desired(*change)
            except Exception as e:
                log.warning("poll error: %s", e)
            time.sleep(self.cfg["poll_seconds"])

    def _on_state_change(self):
        """Called from state changes — update the GUI."""
        if self.root:
            try:
                self.root.after(0, self._update_status_display)
            except Exception:
                pass

    def _show_window(self):
        if self.root:
            try:
                self.root.after(0, lambda: (self.root.deiconify(), self.root.lift(), self.root.focus_force()))
            except Exception:
                pass

    def _hide_window(self):
        if self.root:
            self.root.withdraw()

    def _quit(self):
        self.running = False
        if self.tray:
            self.tray.destroy()
        if self.server:
            self.server.shutdown()
        if self.root:
            try:
                self.root.after(0, self.root.destroy)
            except Exception:
                pass

    # ----- GUI Creation -----
    def _create_gui(self):
        self.root = tk.Tk()
        self.root.title("MAD60 AutoSwitch")
        self.root.geometry("720x680")
        self.root.minsize(600, 500)
        self.root.configure(bg=COLORS["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self._hide_window)

        # Try to set icon
        if os.path.exists(ICON_PATH):
            try:
                self.root.iconbitmap(ICON_PATH)
            except Exception:
                pass

        # Configure ttk styles
        style = ttk.Style()
        style.theme_use("clam")

        style.configure("Dark.TFrame", background=COLORS["bg"])
        style.configure("Card.TFrame", background=COLORS["bg_card"], relief="flat")
        style.configure("Secondary.TFrame", background=COLORS["bg_secondary"])

        style.configure("Title.TLabel",
                         background=COLORS["bg"],
                         foreground=COLORS["accent"],
                         font=("Segoe UI", 18, "bold"))
        style.configure("Subtitle.TLabel",
                         background=COLORS["bg"],
                         foreground=COLORS["text_dim"],
                         font=("Segoe UI", 10))
        style.configure("Dark.TLabel",
                         background=COLORS["bg"],
                         foreground=COLORS["text"],
                         font=("Segoe UI", 10))
        style.configure("Card.TLabel",
                         background=COLORS["bg_card"],
                         foreground=COLORS["text"],
                         font=("Segoe UI", 10))
        style.configure("CardBright.TLabel",
                         background=COLORS["bg_card"],
                         foreground=COLORS["text_bright"],
                         font=("Segoe UI", 11, "bold"))
        style.configure("Status.TLabel",
                         background=COLORS["bg_card"],
                         foreground=COLORS["success"],
                         font=("Segoe UI", 11, "bold"))
        style.configure("StatusWarn.TLabel",
                         background=COLORS["bg_card"],
                         foreground=COLORS["warning"],
                         font=("Segoe UI", 11, "bold"))

        style.configure("Accent.TButton",
                         background=COLORS["button_bg"],
                         foreground=COLORS["text_bright"],
                         font=("Segoe UI", 10, "bold"),
                         borderwidth=0,
                         padding=(12, 6))
        style.map("Accent.TButton",
                   background=[("active", COLORS["button_hover"])])

        style.configure("Secondary.TButton",
                         background=COLORS["bg_secondary"],
                         foreground=COLORS["text"],
                         font=("Segoe UI", 9),
                         borderwidth=1,
                         padding=(8, 4))
        style.map("Secondary.TButton",
                   background=[("active", COLORS["border"])])

        # Treeview styles
        style.configure("Dark.Treeview",
                         background=COLORS["bg_secondary"],
                         foreground=COLORS["text"],
                         fieldbackground=COLORS["bg_secondary"],
                         borderwidth=0,
                         font=("Segoe UI", 10),
                         rowheight=32)
        style.configure("Dark.Treeview.Heading",
                         background=COLORS["bg_card"],
                         foreground=COLORS["accent"],
                         font=("Segoe UI", 10, "bold"),
                         borderwidth=0)
        style.map("Dark.Treeview",
                   background=[("selected", COLORS["accent"])],
                   foreground=[("selected", COLORS["text_bright"])])

        # Combobox
        style.configure("Dark.TCombobox",
                         fieldbackground=COLORS["input_bg"],
                         background=COLORS["bg_secondary"],
                         foreground=COLORS["text"],
                         arrowcolor=COLORS["accent"],
                         borderwidth=1)

        # Main container with padding
        main_frame = ttk.Frame(self.root, style="Dark.TFrame", padding=20)
        main_frame.pack(fill="both", expand=True)

        # ---- Header ----
        header = ttk.Frame(main_frame, style="Dark.TFrame")
        header.pack(fill="x", pady=(0, 15))

        ttk.Label(header, text="⌨  MAD60 AutoSwitch", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="Keyboard Profile Auto-Switcher", style="Subtitle.TLabel").pack(side="left", padx=(12, 0), pady=(6, 0))

        # ---- Status Card ----
        status_card = ttk.Frame(main_frame, style="Card.TFrame", padding=15)
        status_card.pack(fill="x", pady=(0, 15))

        status_header = ttk.Frame(status_card, style="Card.TFrame")
        status_header.pack(fill="x")
        ttk.Label(status_header, text="STATUS", style="Card.TLabel",
                   font=("Segoe UI", 8, "bold")).pack(side="left")

        status_grid = ttk.Frame(status_card, style="Card.TFrame")
        status_grid.pack(fill="x", pady=(8, 0))

        # Active Profile
        col1 = ttk.Frame(status_grid, style="Card.TFrame")
        col1.pack(side="left", expand=True, fill="x")
        ttk.Label(col1, text="Active Profile", style="Card.TLabel").pack(anchor="w")
        self.lbl_profile = ttk.Label(col1, text="—", style="CardBright.TLabel")
        self.lbl_profile.pack(anchor="w", pady=(2, 0))

        # Reason
        col2 = ttk.Frame(status_grid, style="Card.TFrame")
        col2.pack(side="left", expand=True, fill="x")
        ttk.Label(col2, text="Reason", style="Card.TLabel").pack(anchor="w")
        self.lbl_reason = ttk.Label(col2, text="—", style="Card.TLabel")
        self.lbl_reason.pack(anchor="w", pady=(2, 0))

        # Driver Connection
        col3 = ttk.Frame(status_grid, style="Card.TFrame")
        col3.pack(side="left", expand=True, fill="x")
        ttk.Label(col3, text="Driver Page", style="Card.TLabel").pack(anchor="w")
        self.lbl_driver = ttk.Label(col3, text="⬤ Waiting...", style="StatusWarn.TLabel")
        self.lbl_driver.pack(anchor="w", pady=(2, 0))

        # ---- Notebook for tabs ----
        notebook_frame = ttk.Frame(main_frame, style="Dark.TFrame")
        notebook_frame.pack(fill="both", expand=True)

        style.configure("Dark.TNotebook", background=COLORS["bg"], borderwidth=0)
        style.configure("Dark.TNotebook.Tab",
                         background=COLORS["bg_secondary"],
                         foreground=COLORS["text_dim"],
                         font=("Segoe UI", 10),
                         padding=(14, 6),
                         borderwidth=0)
        style.map("Dark.TNotebook.Tab",
                   background=[("selected", COLORS["bg_card"])],
                   foreground=[("selected", COLORS["text_bright"])])

        self.notebook = ttk.Notebook(notebook_frame, style="Dark.TNotebook")
        self.notebook.pack(fill="both", expand=True)

        # Tab 1: Game Mappings
        self._create_games_tab()

        # Tab 2: Profiles
        self._create_profiles_tab()

        # Tab 3: Settings
        self._create_settings_tab()

        # Update status periodically
        self._periodic_status_update()

    def _create_games_tab(self):
        tab = ttk.Frame(self.notebook, style="Dark.TFrame", padding=12)
        self.notebook.add(tab, text="  🎮 Game Mappings  ")

        # Toolbar
        toolbar = ttk.Frame(tab, style="Dark.TFrame")
        toolbar.pack(fill="x", pady=(0, 8))

        ttk.Button(toolbar, text="+ Add Game", style="Accent.TButton",
                    command=self._add_game_dialog).pack(side="left")
        ttk.Button(toolbar, text="Remove Selected", style="Secondary.TButton",
                    command=self._remove_game).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="Edit Selected", style="Secondary.TButton",
                    command=self._edit_game_dialog).pack(side="left", padx=(8, 0))

        # Treeview
        tree_frame = ttk.Frame(tab, style="Dark.TFrame")
        tree_frame.pack(fill="both", expand=True)

        columns = ("name", "profile", "processes")
        self.game_tree = ttk.Treeview(tree_frame, columns=columns, show="headings",
                                       style="Dark.Treeview", selectmode="browse")
        self.game_tree.heading("name", text="Game Name")
        self.game_tree.heading("profile", text="Profile")
        self.game_tree.heading("processes", text="Processes")
        self.game_tree.column("name", width=160, minwidth=100)
        self.game_tree.column("profile", width=140, minwidth=80)
        self.game_tree.column("processes", width=300, minwidth=150)

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.game_tree.yview)
        self.game_tree.configure(yscrollcommand=scrollbar.set)

        self.game_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._refresh_game_tree()

    def _create_profiles_tab(self):
        tab = ttk.Frame(self.notebook, style="Dark.TFrame", padding=12)
        self.notebook.add(tab, text="  📋 Profiles  ")

        # Info
        info_frame = ttk.Frame(tab, style="Card.TFrame", padding=12)
        info_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(info_frame, text="Profiles detected from FGG Hub IndexedDB (hub.fgg.com.cn)",
                   style="Card.TLabel").pack(anchor="w")
        ttk.Label(info_frame,
                   text="These are profile names found in your browser's local data for the keyboard driver.",
                   style="Card.TLabel", font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 0))

        # Toolbar
        toolbar = ttk.Frame(tab, style="Dark.TFrame")
        toolbar.pack(fill="x", pady=(0, 8))

        ttk.Button(toolbar, text="🔄 Re-scan Profiles", style="Accent.TButton",
                    command=self._rescan_profiles).pack(side="left")

        # Profile list
        self.profile_listbox = tk.Listbox(
            tab,
            bg=COLORS["bg_secondary"],
            fg=COLORS["text"],
            selectbackground=COLORS["accent"],
            selectforeground=COLORS["text_bright"],
            font=("Segoe UI", 11),
            borderwidth=0,
            highlightthickness=1,
            highlightcolor=COLORS["border"],
            highlightbackground=COLORS["border"],
            relief="flat",
        )
        self.profile_listbox.pack(fill="both", expand=True)

        self._refresh_profile_list()

    def _create_settings_tab(self):
        tab = ttk.Frame(self.notebook, style="Dark.TFrame", padding=12)
        self.notebook.add(tab, text="  ⚙ Settings  ")

        # Settings grid
        settings = ttk.Frame(tab, style="Card.TFrame", padding=15)
        settings.pack(fill="x", pady=(0, 10))

        row = 0
        ttk.Label(settings, text="Default Profile:", style="Card.TLabel").grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        self.default_profile_var = tk.StringVar(value=self.cfg.get("default_profile", "normal"))
        self.default_profile_combo = ttk.Combobox(
            settings, textvariable=self.default_profile_var,
            values=self.scanned_profiles or [],
            style="Dark.TCombobox", width=25)
        self.default_profile_combo.grid(row=row, column=1, sticky="w", pady=5)

        row += 1
        ttk.Label(settings, text="Poll Interval (seconds):", style="Card.TLabel").grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        self.poll_var = tk.StringVar(value=str(self.cfg.get("poll_seconds", 2)))
        tk.Entry(settings, textvariable=self.poll_var, width=10,
                  bg=COLORS["input_bg"], fg=COLORS["text"], insertbackground=COLORS["text"],
                  font=("Segoe UI", 10), borderwidth=1, relief="flat").grid(
            row=row, column=1, sticky="w", pady=5)

        row += 1
        ttk.Label(settings, text="Exit Grace (seconds):", style="Card.TLabel").grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        self.grace_var = tk.StringVar(value=str(self.cfg.get("exit_grace_seconds", 15)))
        tk.Entry(settings, textvariable=self.grace_var, width=10,
                  bg=COLORS["input_bg"], fg=COLORS["text"], insertbackground=COLORS["text"],
                  font=("Segoe UI", 10), borderwidth=1, relief="flat").grid(
            row=row, column=1, sticky="w", pady=5)

        row += 1
        ttk.Label(settings, text="HTTP Port:", style="Card.TLabel").grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        self.port_var = tk.StringVar(value=str(self.cfg.get("port", 47831)))
        tk.Entry(settings, textvariable=self.port_var, width=10,
                  bg=COLORS["input_bg"], fg=COLORS["text"], insertbackground=COLORS["text"],
                  font=("Segoe UI", 10), borderwidth=1, relief="flat").grid(
            row=row, column=1, sticky="w", pady=5)

        row += 1
        ttk.Label(settings, text="Apply Button Text:", style="Card.TLabel").grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        self.apply_btn_var = tk.StringVar(value=self.cfg.get("apply_button_text", ""))
        tk.Entry(settings, textvariable=self.apply_btn_var, width=28,
                  bg=COLORS["input_bg"], fg=COLORS["text"], insertbackground=COLORS["text"],
                  font=("Segoe UI", 10), borderwidth=1, relief="flat").grid(
            row=row, column=1, sticky="w", pady=5)

        # Save button
        btn_frame = ttk.Frame(tab, style="Dark.TFrame")
        btn_frame.pack(fill="x", pady=(10, 0))
        ttk.Button(btn_frame, text="💾 Save Settings", style="Accent.TButton",
                    command=self._save_settings).pack(side="left")

        # Startup controls
        startup_frame = ttk.Frame(tab, style="Card.TFrame", padding=12)
        startup_frame.pack(fill="x", pady=(15, 0))
        ttk.Label(startup_frame, text="WINDOWS STARTUP",
                   style="Card.TLabel", font=("Segoe UI", 8, "bold")).pack(anchor="w")
        startup_btns = ttk.Frame(startup_frame, style="Card.TFrame")
        startup_btns.pack(fill="x", pady=(8, 0))
        ttk.Button(startup_btns, text="Install Startup", style="Secondary.TButton",
                    command=self._install_startup).pack(side="left")
        ttk.Button(startup_btns, text="Remove Startup", style="Secondary.TButton",
                    command=self._remove_startup).pack(side="left", padx=(8, 0))

    # ----- Actions -----
    def _refresh_game_tree(self):
        for item in self.game_tree.get_children():
            self.game_tree.delete(item)
        for g in self.cfg.get("games", []):
            procs = g["processes"] if isinstance(g["processes"], list) else [g["processes"]]
            self.game_tree.insert("", "end", values=(
                g.get("name", ""),
                g.get("profile", ""),
                ", ".join(procs),
            ))

    def _refresh_profile_list(self):
        self.profile_listbox.delete(0, "end")
        for p in self.scanned_profiles:
            self.profile_listbox.insert("end", f"  ●  {p}")

    def _rescan_profiles(self):
        self._scan_profiles()
        self._refresh_profile_list()
        # Update default profile combobox
        self.default_profile_combo.configure(values=self.scanned_profiles)
        tkmb.showinfo("Scan Complete",
                       f"Found {len(self.scanned_profiles)} profile(s):\n\n" +
                       "\n".join(self.scanned_profiles) if self.scanned_profiles else "No profiles found.",
                       parent=self.root)

    def _add_game_dialog(self):
        self._game_dialog(None)

    def _edit_game_dialog(self):
        sel = self.game_tree.selection()
        if not sel:
            tkmb.showwarning("No Selection", "Select a game mapping to edit.", parent=self.root)
            return
        idx = self.game_tree.index(sel[0])
        self._game_dialog(idx)

    def _game_dialog(self, edit_index: int | None):
        dlg = tk.Toplevel(self.root)
        dlg.title("Edit Game" if edit_index is not None else "Add Game")
        dlg.geometry("420x340")
        dlg.configure(bg=COLORS["bg"])
        dlg.transient(self.root)
        dlg.grab_set()

        # Try to set icon
        if os.path.exists(ICON_PATH):
            try:
                dlg.iconbitmap(ICON_PATH)
            except Exception:
                pass

        frame = tk.Frame(dlg, bg=COLORS["bg"], padx=20, pady=20)
        frame.pack(fill="both", expand=True)

        # Pre-fill if editing
        game_data = None
        if edit_index is not None and edit_index < len(self.cfg["games"]):
            game_data = self.cfg["games"][edit_index]

        # Name
        tk.Label(frame, text="Game Name:", bg=COLORS["bg"], fg=COLORS["text"],
                  font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 3))
        name_var = tk.StringVar(value=game_data["name"] if game_data else "")
        tk.Entry(frame, textvariable=name_var, width=40,
                  bg=COLORS["input_bg"], fg=COLORS["text"], insertbackground=COLORS["text"],
                  font=("Segoe UI", 10), borderwidth=1, relief="flat").pack(fill="x", pady=(0, 10))

        # Profile
        tk.Label(frame, text="Profile:", bg=COLORS["bg"], fg=COLORS["text"],
                  font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 3))
        profile_var = tk.StringVar(value=game_data["profile"] if game_data else "")
        profile_combo = ttk.Combobox(frame, textvariable=profile_var,
                                      values=self.scanned_profiles, width=38,
                                      style="Dark.TCombobox")
        profile_combo.pack(fill="x", pady=(0, 10))

        # Processes
        tk.Label(frame, text="Process Names (comma-separated):", bg=COLORS["bg"], fg=COLORS["text"],
                  font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 3))
        procs_val = ""
        if game_data:
            p = game_data["processes"]
            procs_val = ", ".join(p) if isinstance(p, list) else p
        procs_var = tk.StringVar(value=procs_val)
        tk.Entry(frame, textvariable=procs_var, width=40,
                  bg=COLORS["input_bg"], fg=COLORS["text"], insertbackground=COLORS["text"],
                  font=("Segoe UI", 10), borderwidth=1, relief="flat").pack(fill="x", pady=(0, 10))

        # Tip
        tk.Label(frame, text="Tip: Use --list or check Task Manager for process names",
                  bg=COLORS["bg"], fg=COLORS["text_dim"], font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 10))

        # Buttons
        btn_frame = tk.Frame(frame, bg=COLORS["bg"])
        btn_frame.pack(fill="x")

        def on_save():
            name = name_var.get().strip()
            profile = profile_var.get().strip()
            procs = [p.strip().lower() for p in procs_var.get().split(",") if p.strip()]
            if not name or not profile or not procs:
                tkmb.showwarning("Missing Fields", "All fields are required.", parent=dlg)
                return
            entry = {"name": name, "profile": profile, "processes": procs}
            if edit_index is not None:
                self.cfg["games"][edit_index] = entry
            else:
                self.cfg["games"].append(entry)
            save_config(self.cfg)
            self._refresh_game_tree()
            # Recreate watcher with updated config
            self.watcher = Watcher(self.cfg)
            dlg.destroy()

        save_btn = tk.Button(btn_frame, text="Save", bg=COLORS["button_bg"], fg=COLORS["text_bright"],
                              font=("Segoe UI", 10, "bold"), borderwidth=0, padx=16, pady=4,
                              activebackground=COLORS["button_hover"], command=on_save)
        save_btn.pack(side="left")

        cancel_btn = tk.Button(btn_frame, text="Cancel", bg=COLORS["bg_secondary"], fg=COLORS["text"],
                                font=("Segoe UI", 10), borderwidth=0, padx=16, pady=4,
                                command=dlg.destroy)
        cancel_btn.pack(side="left", padx=(8, 0))

    def _remove_game(self):
        sel = self.game_tree.selection()
        if not sel:
            tkmb.showwarning("No Selection", "Select a game mapping to remove.", parent=self.root)
            return
        idx = self.game_tree.index(sel[0])
        game = self.cfg["games"][idx]
        if tkmb.askyesno("Remove Game",
                          f"Remove \"{game['name']}\" ({game['profile']})?\nThis will update config.json.",
                          parent=self.root):
            del self.cfg["games"][idx]
            save_config(self.cfg)
            self._refresh_game_tree()
            self.watcher = Watcher(self.cfg)

    def _save_settings(self):
        try:
            self.cfg["default_profile"] = self.default_profile_var.get().strip() or "normal"
            self.cfg["poll_seconds"] = max(1, int(self.poll_var.get()))
            self.cfg["exit_grace_seconds"] = max(0, int(self.grace_var.get()))
            self.cfg["port"] = int(self.port_var.get())
            self.cfg["apply_button_text"] = self.apply_btn_var.get().strip()
            save_config(self.cfg)
            self.watcher = Watcher(self.cfg)
            self.state.apply_button_text = self.cfg["apply_button_text"]
            tkmb.showinfo("Saved", "Settings saved to config.json.", parent=self.root)
        except ValueError as e:
            tkmb.showerror("Invalid Input", f"Please enter valid numbers.\n{e}", parent=self.root)

    def _install_startup(self):
        try:
            appdata = os.environ.get("APPDATA", "")
            if not appdata:
                tkmb.showerror("Error", "APPDATA not set.", parent=self.root)
                return
            startup_dir = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
            vbs_path = os.path.join(startup_dir, "MAD60AutoProfile.vbs")

            if getattr(sys, "frozen", False):
                # Running as exe
                exe_path = sys.executable
                vbs_content = (
                    'Set sh = CreateObject("WScript.Shell")\r\n'
                    'sh.CurrentDirectory = "%s"\r\n'
                    'sh.Run """%s""", 0, False\r\n' % (os.path.dirname(exe_path), exe_path)
                )
            else:
                # Running as script
                exe_dir = os.path.dirname(sys.executable)
                pythonw = os.path.join(exe_dir, "pythonw.exe")
                if not os.path.exists(pythonw):
                    pythonw = sys.executable
                script_path = os.path.abspath(__file__)
                vbs_content = (
                    'Set sh = CreateObject("WScript.Shell")\r\n'
                    'sh.CurrentDirectory = "%s"\r\n'
                    'sh.Run """%s"" ""%s""", 0, False\r\n' % (os.path.dirname(script_path), pythonw, script_path)
                )

            with open(vbs_path, "w", encoding="utf-8", newline="") as f:
                f.write(vbs_content)
            tkmb.showinfo("Installed", f"Startup entry installed:\n{vbs_path}", parent=self.root)
        except Exception as e:
            tkmb.showerror("Error", str(e), parent=self.root)

    def _remove_startup(self):
        try:
            appdata = os.environ.get("APPDATA", "")
            vbs_path = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "MAD60AutoProfile.vbs")
            if os.path.exists(vbs_path):
                os.remove(vbs_path)
                tkmb.showinfo("Removed", "Startup entry removed.", parent=self.root)
            else:
                tkmb.showinfo("Nothing to Remove", "No startup entry found.", parent=self.root)
        except Exception as e:
            tkmb.showerror("Error", str(e), parent=self.root)

    def _update_status_display(self):
        if not self.root:
            return
        try:
            profile = self.state.desired or "—"
            reason = self.state.reason or "—"
            self.lbl_profile.configure(text=profile)
            self.lbl_reason.configure(text=reason)

            age = time.time() - self.state.last_seen if self.state.last_seen else None
            if age is not None and age < DRIVER_GONE_AFTER:
                self.lbl_driver.configure(text="⬤ Connected", style="Status.TLabel")
            else:
                self.lbl_driver.configure(text="⬤ Disconnected", style="StatusWarn.TLabel")

            # Update tray tooltip
            if self.tray:
                self.tray.update_tooltip(f"MAD60: {profile}")
        except Exception:
            pass

    def _periodic_status_update(self):
        if not self.running:
            return
        self._update_status_display()
        self.root.after(2000, self._periodic_status_update)


# =========================================================================
#  Entry point
# =========================================================================
def main():
    # Prevent multiple instances using a named mutex
    try:
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "MAD60AutoSwitchMutex")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            tkmb.showerror("MAD60 AutoSwitch", "Another instance is already running.")
            sys.exit(1)
    except Exception:
        pass

    app = MAD60App()


if __name__ == "__main__":
    main()
