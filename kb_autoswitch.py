#!/usr/bin/env python3
"""
MAD60 HE auto profile switcher  (helper half)

Watches for running game processes and tells the browser extension which
keyboard profile to select in the MadLions web driver. The extension does the
actual clicking inside the driver page; this script never touches the keyboard.

Usage:
  python kb_autoswitch.py                       run (reads config.json)
  python kb_autoswitch.py --force lol           test: select a profile now, no game needed
  python kb_autoswitch.py --list                list running process names (to find a game's .exe)
  python kb_autoswitch.py --set-driver-url URL  tell the extension which site is the driver
  python kb_autoswitch.py --install-startup     start automatically when you sign in to Windows
  python kb_autoswitch.py --remove-startup
"""
from __future__ import annotations

import argparse
import json
import logging
import ntpath
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
MANIFEST_PATH = os.path.join(HERE, "extension", "manifest.json")
LOG_PATH = os.path.join(HERE, "kb_autoswitch.log")
LONG_POLL_SECONDS = 25          # how long /next holds a request open waiting for a change
DRIVER_GONE_AFTER = 60          # seconds without a poll before we consider the driver page gone
STARTUP_FILE = "MAD60AutoProfile.vbs"

log = logging.getLogger("kb")

DEFAULTS = {
    "port": 47831,               # must match extension/background.js
    "poll_seconds": 2,
    "exit_grace_seconds": 15,    # wait this long after a game closes before going back to default
    "default_profile": "normal",
    "apply_button_text": "",     # optional: text of an Apply/Save button to click after selecting a profile
    "games": [],
}


# --------------------------------------------------------------------------- config
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
        games.append({"name": g.get("name") or g["profile"], "profile": g["profile"], "processes": procs})
    cfg["games"] = games
    return cfg


# --------------------------------------------------------------------------- process list
def _list_processes_windows() -> set:
    import ctypes
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


def _list_processes_proc() -> set:
    """Linux/macOS-less fallback, mainly so the logic can be tested off Windows."""
    names = set()
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    first = f.read().split(b"\0")[0].decode("utf-8", "replace")
                if first:
                    names.add(os.path.basename(first.replace("\\", "/")).lower())
                with open("/proc/%s/comm" % pid, encoding="utf-8", errors="replace") as f:
                    names.add(f.read().strip().lower())
            except OSError:
                continue
    except OSError:
        pass
    return names


def list_processes() -> set:
    return _list_processes_windows() if sys.platform == "win32" else _list_processes_proc()


# --------------------------------------------------------------------------- decision logic
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
            # if several games are open, the one launched most recently wins
            g = max(active, key=lambda x: self.first_seen[x["name"]])
            self.idle_since = None
            new, reason = g["profile"], "%s is running" % g["name"]
        else:
            if self.idle_since is None:
                self.idle_since = now
            if self.target not in (None, self.default) and now - self.idle_since < self.grace:
                return None  # a game just closed; hold the profile for a moment
            new, reason = self.default, "no game running"

        if new != self.target:
            self.target = new
            return new, reason
        return None


# --------------------------------------------------------------------------- shared state + HTTP
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

    def set_desired(self, profile: str, reason: str):
        with self.cond:
            if profile == self.desired:
                return
            self.desired, self.reason = profile, reason
            self.rev += 1
            self.cond.notify_all()
        log.info("target profile -> %s (%s)", profile, reason)

    def snapshot(self) -> dict:
        return {
            "rev": self.rev,
            "profile": self.desired,
            "pending": self.desired is not None and self.desired != self.applied,
            "applyButtonText": self.apply_button_text,
        }

    def report(self, payload: dict):
        profile, ok, detail = payload.get("profile"), bool(payload.get("ok")), str(payload.get("detail", ""))
        with self.cond:
            if ok and profile == self.desired:
                self.applied = profile
            self.last_report = {"profile": profile, "ok": ok, "detail": detail, "at": time.time()}
            key = (profile, ok, detail)
            first_time = key != self._last_logged_report
            self._last_logged_report = key
        if first_time:
            (log.info if ok else log.warning)("driver page: %s '%s' (%s)", "selected" if ok else "FAILED to select", profile, detail)


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default per-request logging
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
            return host in ("127.0.0.1", "localhost")  # blocks DNS-rebinding style access

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


# --------------------------------------------------------------------------- commands
def setup_logging(console: bool = True) -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=200_000, backupCount=1, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if console and sys.stdout is not None:   # pythonw has no stdout
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def serve(cfg: dict, force: str | None = None, lister=list_processes) -> None:
    state = State(cfg["apply_button_text"])
    try:
        server = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), make_handler(state))
    except OSError as e:
        log.error("Could not open port %s (%s). Is another copy already running?", cfg["port"], e)
        sys.exit(1)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("helper running on 127.0.0.1:%s", cfg["port"])

    if force:
        log.info("FORCED mode: game detection is off")
        state.set_desired(force, "forced from command line")
        while True:
            time.sleep(3600)

    for g in cfg["games"]:
        log.info("watching for %s -> profile '%s'", " / ".join(g["processes"]), g["profile"])
    watcher = Watcher(cfg)
    while True:
        try:
            change = watcher.tick(lister(), time.time())
            if change:
                state.set_desired(*change)
        except Exception as e:  # never let a bad poll kill the loop
            log.warning("poll error: %s", e)
        time.sleep(cfg["poll_seconds"])


def set_driver_url(value: str) -> None:
    if value.lower() == "any":
        pattern = "https://*/*"
        print("WARNING: the extension will be allowed to run on every https site.")
        print("         (It only acts on a page titled MADLIONS, but a narrow URL is safer.)")
    else:
        u = urlparse(value if "://" in value else "https://" + value)
        if u.scheme not in ("http", "https") or not u.hostname:
            sys.exit("That doesn't look like a URL: %r" % value)
        pattern = "%s://%s/*" % (u.scheme, u.hostname)
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        manifest = json.load(f)
    manifest["content_scripts"][0]["matches"] = [pattern]
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print("Extension will run on: %s" % pattern)
    print("Now reload the extension (chrome://extensions -> the refresh icon), then close and reopen the driver window.")


def startup_path() -> str:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        sys.exit("APPDATA is not set - startup install only works on Windows.")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", STARTUP_FILE)


def vbs_launcher(pythonw: str, script: str) -> str:
    return (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        'sh.CurrentDirectory = "%s"\r\n'
        'sh.Run """%s"" ""%s""", 0, False\r\n' % (ntpath.dirname(script), pythonw, script)
    )


def install_startup() -> None:
    exe_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(exe_dir, "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    path = startup_path()
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(vbs_launcher(pythonw, os.path.abspath(__file__)))
    print("Installed: %s" % path)
    print("The helper will now start hidden each time you sign in.")


def remove_startup() -> None:
    path = startup_path()
    if os.path.exists(path):
        os.remove(path)
        print("Removed %s" % path)
    else:
        print("Nothing to remove.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Auto-select the MAD60 HE profile based on the running game.")
    ap.add_argument("--force", metavar="PROFILE", help="select PROFILE now and skip game detection (for testing)")
    ap.add_argument("--list", action="store_true", help="print running process names and exit")
    ap.add_argument("--set-driver-url", metavar="URL", help="site the MadLions driver runs on, or 'any'")
    ap.add_argument("--install-startup", action="store_true")
    ap.add_argument("--remove-startup", action="store_true")
    args = ap.parse_args()

    if args.list:
        print("\n".join(sorted(list_processes())))
        return
    if args.set_driver_url:
        return set_driver_url(args.set_driver_url)
    if args.install_startup:
        return install_startup()
    if args.remove_startup:
        return remove_startup()

    setup_logging()
    try:
        cfg = load_config()
    except (OSError, ValueError) as e:
        log.error("config.json problem: %s", e)
        sys.exit(1)
    try:
        serve(cfg, force=args.force)
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
