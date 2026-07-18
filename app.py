"""
Google Earth 3D -> RenderDoc capture (.rdc), fully automated.

Reproduces the capture stage of the "Google Earth into 3ds Max" workflow up to
producing the RenderDoc capture file, with no manual RenderDoc clicking:

  1. Launch Chrome paused at its GPU-startup dialog, with the flags RenderDoc
     needs (RENDERDOC_HOOK_EGL=0, --disable-gpu-sandbox, D3D11 ANGLE).
  2. A qrenderdoc worker (capture_worker.py) injects RenderDoc into the paused
     GPU process *before* it creates its D3D11 device, connects target control,
     and dismisses the dialog so the process resumes already hooked.
  3. This app drives Chrome (via the DevTools port) to Google Earth at your
     location and lets the 3D tiles stream in.
  4. The worker triggers a frame capture over target control; the .rdc lands in
     your output folder.

The .rdc then imports in Blender 4.1 with the Maps Models Importer v0.7.0 add-on
(File > Import > Google Maps Capture).

REQUIRES RenderDoc 1.25 (bundled in ./tools, or point the path at your own).
Newer RenderDoc (e.g. 1.45) crashes Chrome's GPU process on WebGL and its
captures aren't read by the Blender importer.

Standard library only (uses the DevTools port over plain sockets).
"""

import base64
import ctypes
import json
import os
import queue
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import winreg
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_TITLE = "Google Earth 3D → RenderDoc capture"
PROFILE_DIR_NAME = "earth_rdc_profile"
CAPTURE_BASENAME = "earth"
CREATE_NO_WINDOW = 0x08000000
DEBUG_PORT = 9390

import sys

if getattr(sys, 'frozen', False):
    HERE = sys._MEIPASS
    EXEC_DIR = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    EXEC_DIR = HERE

WORKER = os.path.join(HERE, "capture_worker.py")
user32 = ctypes.windll.user32


# ---------------------------------------------------------------- detection

def find_chrome():
    try:
        key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            path, _ = winreg.QueryValueEx(k, None)
            if path and os.path.isfile(path):
                return path
    except OSError:
        pass
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    return next((p for p in candidates if os.path.isfile(p)), "")


def find_qrenderdoc():
    """Prefer the bundled RenderDoc 1.25, else fall back to a system install."""
    bundled = os.path.join(EXEC_DIR, "tools", "RenderDoc_1.25",
                           "RenderDoc_1.25_64", "qrenderdoc.exe")
    candidates = [
        bundled,
        os.path.expandvars(r"%ProgramFiles%\RenderDoc\qrenderdoc.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\RenderDoc\qrenderdoc.exe"),
    ]
    return next((p for p in candidates if os.path.isfile(p)), "")


def get_file_version(path):
    script = "(Get-Item '%s').VersionInfo.FileVersion" % path
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=30,
            creationflags=CREATE_NO_WINDOW).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---------------------------------------------------------------- geocoding

def geocode(location):
    """Return (lat, lon). Accepts 'lat, lon' directly or a place name (via
    OpenStreetMap Nominatim). Raises ValueError if it can't resolve."""
    parts = [p.strip() for p in location.split(",")]
    if len(parts) == 2:
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            pass
    q = urllib.parse.quote(location.strip())
    url = ("https://nominatim.openstreetmap.org/search?q=%s&format=json&limit=1" % q)
    req = urllib.request.Request(url, headers={"User-Agent": "earth-rdc/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.load(r)
    if not data:
        raise ValueError("Could not find a location named %r" % location)
    return float(data[0]["lat"]), float(data[0]["lon"])


def google_earth_url(lat, lon, tilt=60, dist=700):
    # @lat,lon,altitude(a),distance(d),fov(y),heading(h),tilt(t),roll(r)
    return ("https://earth.google.com/web/@%.7f,%.7f,150a,%dd,35y,0h,%dt,0r"
            % (lat, lon, dist, tilt))


# ---------------------------------------------------------------- chrome + CDP

def chrome_cmd(chrome, profile):
    return [
        chrome,
        "--user-data-dir=%s" % profile,
        "--remote-debugging-port=%d" % DEBUG_PORT,
        "--disable-gpu-sandbox",     # let RenderDoc reach the GPU process
        "--use-angle=d3d11",         # the backend RenderDoc captures
        "--gpu-startup-dialog",      # pause GPU proc so we inject before D3D init
        "--disable-gpu-watchdog",    # don't kill the (paused) GPU process
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]


def hooked_env():
    env = os.environ.copy()
    env["RENDERDOC_HOOK_EGL"] = "0"
    return env


def gpu_pid(profile_marker):
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like '*" + profile_marker + "*' -and "
        "$_.CommandLine -like '*--type=gpu-process*' } | "
        "ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=30,
                             creationflags=CREATE_NO_WINDOW).stdout
        ids = [int(x) for x in out.split() if x.strip().isdigit()]
        return ids[0] if ids else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def kill_profile_chrome(profile_marker):
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like '*" + profile_marker + "*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-Command", script],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)


def _ws(port):
    tabs = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % port,
                                            timeout=10))
    page = next(t for t in tabs if t["type"] == "page")
    host = page["webSocketDebuggerUrl"].split("://")[1]
    hp, path = host.split("/", 1)
    hn, pt = hp.split(":")
    sk = socket.create_connection((hn, int(pt)), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    sk.send(("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
             "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
             "Sec-WebSocket-Version: 13\r\n\r\n" % (path, hp, key)).encode())
    sk.recv(4096)
    return sk


def _ws_send(sk, obj):
    payload = json.dumps(obj).encode()
    n = len(payload)
    mask = os.urandom(4)
    hdr = bytearray([0x81])
    if n < 126:
        hdr += bytes([0x80 | n])
    elif n < 65536:
        hdr += bytes([0x80 | 126, (n >> 8) & 0xff, n & 0xff])
    else:
        hdr += bytes([0x80 | 127]) + n.to_bytes(8, "big")
    hdr += mask
    sk.send(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _ws_recv(sk):
    sk.settimeout(3)
    try:
        data = sk.recv(65536)
    except socket.timeout:
        return ""
    if len(data) < 2:
        return ""
    ln = data[1] & 0x7f
    idx = 4 if ln == 126 else (10 if ln == 127 else 2)
    return data[idx:].decode("utf-8", "replace")


def cdp_navigate(port, url):
    sk = _ws(port)
    _ws_send(sk, {"id": 1, "method": "Page.navigate", "params": {"url": url}})
    time.sleep(0.4)
    sk.close()


def cdp_viewport(port):
    """Return (width, height) of the page viewport in CSS pixels."""
    sk = _ws(port)
    try:
        _ws_send(sk, {"id": 1, "method": "Runtime.evaluate",
                      "params": {"expression": "[innerWidth, innerHeight]",
                                 "returnByValue": True}})
        out = _ws_recv(sk)
        vals = json.loads(out)["result"]["result"]["value"]
        return int(vals[0]), int(vals[1])
    except Exception:
        return 1200, 800
    finally:
        sk.close()


def orbit_camera(port, stop_event):
    """Gently orbit the Google Earth camera with a small circular mouse drag so
    the tile cache is invalidated and every visible tile's geometry is
    re-submitted while the capture fires. Net displacement per revolution ~0."""
    import math
    w, h = cdp_viewport(port)
    cx, cy = w / 2.0, h / 2.0
    r = min(w, h) * 0.06        # small radius
    sk = _ws(port)
    try:
        _ws_send(sk, {"id": 1, "method": "Input.dispatchMouseEvent",
                      "params": {"type": "mousePressed", "x": cx + r, "y": cy,
                                 "button": "left", "buttons": 1, "clickCount": 1}})
        ang = 0.0
        mid = 2
        sk.settimeout(0.0) # non-blocking for drain
        while not stop_event.is_set():
            ang += 0.35
            x = cx + r * math.cos(ang)
            y = cy + r * math.sin(ang)
            _ws_send(sk, {"id": mid, "method": "Input.dispatchMouseEvent",
                          "params": {"type": "mouseMoved", "x": x, "y": y,
                                     "button": "left", "buttons": 1}})
            mid += 1
            
            # Drain the websocket buffer so Chrome doesn't drop the connection
            try:
                while True:
                    sk.recv(65536)
            except Exception:
                pass
                
            time.sleep(0.045)
            
        _ws_send(sk, {"id": mid, "method": "Input.dispatchMouseEvent",
                      "params": {"type": "mouseReleased", "x": x, "y": y,
                                 "button": "left", "buttons": 0, "clickCount": 1}})
    except Exception as e:
        print("Orbit camera error:", e)
    finally:
        try:
            sk.close()
        except Exception:
            pass


def proc_alive(pid):
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-Process -Id %d -EA SilentlyContinue | %% { $_.Id }" % pid],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW).stdout
    return str(pid) in out


def hide_gpu_dialog_once():
    """Find the '--gpu-startup-dialog' message box and hide it (SW_HIDE) so it
    never appears in front of the user. It stays modal (still pausing the GPU
    process); the worker dismisses it with WM_COMMAND once injection is done.
    Returns True if a dialog was hidden."""
    hidden = [False]
    SW_HIDE = 0

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != "#32770":
            return True
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if "gpu" in title.value.lower():
            user32.ShowWindow(hwnd, SW_HIDE)
            hidden[0] = True
    user32.EnumWindows(cb, 0)
    return hidden[0]


def _find_gpu_dialog(pid):
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        p = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if p.value == pid and cls.value == "#32770":
            found.append(hwnd)
            return False
        return True
    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def dismiss_gpu_dialog(pid, attempts=40):
    """Send OK to the (possibly hidden) GPU dialog so the process resumes.
    Retries because the dialog can take a moment to appear."""
    for _ in range(attempts):
        hwnd = _find_gpu_dialog(pid)
        if hwnd:
            user32.SendMessageW(hwnd, 0x0111, 1, 0)  # WM_COMMAND, IDOK
            time.sleep(0.1)
            if _find_gpu_dialog(pid) is None:
                return True
        time.sleep(0.1)
    return False


def unhide_gpu_dialog(pid):
    """Fallback: make the dialog visible again so the user can click OK."""
    hwnd = _find_gpu_dialog(pid)
    if hwnd:
        user32.ShowWindow(hwnd, 5)   # SW_SHOW
        user32.SetForegroundWindow(hwnd)
        return True
    return False


# ------------------------------------------------------------------- the app

class App:
    def __init__(self, root):
        self.root = root
        self.log_queue = queue.Queue()
        self.busy = False
        self.capture_now_event = threading.Event()
        self.build_ui()
        self.root.after(150, self.drain_log)
        self.log("Ready. Enter a location and click '1. Open Google Earth (hooked)'.")
        threading.Thread(target=self.version_check,
                         args=(self.var_qrd.get(),), daemon=True).start()

    def do_capture_now(self):
        self.btn_cap.configure(state="disabled")
        self.capture_now_event.set()

    # ---- UI

    def build_ui(self):
        self.root.title(APP_TITLE)
        self.root.geometry("760x580")
        pad = {"padx": 8, "pady": 4}

        paths = ttk.LabelFrame(self.root, text="Paths")
        paths.pack(fill="x", **pad)
        self.var_qrd = tk.StringVar(value=find_qrenderdoc())
        self.var_chrome = tk.StringVar(value=find_chrome())
        self.var_out = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Documents", "EarthCaptures"))
        self._row(paths, 0, "qrenderdoc.exe (1.25)", self.var_qrd, [("exe", "*.exe")])
        self._row(paths, 1, "chrome.exe", self.var_chrome, [("exe", "*.exe")])
        self._row(paths, 2, "Output folder", self.var_out, None)

        loc = ttk.LabelFrame(self.root, text="Location (Google Earth)")
        loc.pack(fill="x", **pad)
        loc.columnconfigure(1, weight=1)
        ttk.Label(loc, text="Place or 'lat, lon'").grid(row=0, column=0, sticky="w", **pad)
        self.var_loc = tk.StringVar(value="45.4215, -75.6990")
        ttk.Entry(loc, textvariable=self.var_loc).grid(row=0, column=1, sticky="ew", **pad)


        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        self.btn_go = ttk.Button(btns, text="1. Open Google Earth (hooked)",
                                 command=self.start_capture)
        self.btn_go.pack(side="left", padx=4)
        self.btn_cap = ttk.Button(btns, text="2. Capture Now",
                                   command=self.do_capture_now, state="disabled")
        self.btn_cap.pack(side="left", padx=4)
        ttk.Button(btns, text="Open captures folder",
                   command=self.open_out_dir).pack(side="left", padx=4)

        logf = ttk.LabelFrame(self.root, text="Log")
        logf.pack(fill="both", expand=True, **pad)
        self.txt = tk.Text(logf, height=18, wrap="word", state="disabled")
        self.txt.pack(fill="both", expand=True, padx=4, pady=4)

    def _row(self, parent, row, label, var, filetypes):
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=2)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=8, pady=2)

        def browse():
            p = (filedialog.askopenfilename(filetypes=filetypes) if filetypes
                 else filedialog.askdirectory())
            if p:
                var.set(p)
        ttk.Button(parent, text="...", width=3, command=browse)\
            .grid(row=row, column=2, padx=4, pady=2)

    # ---- logging

    def log(self, msg):
        self.log_queue.put(time.strftime("[%H:%M:%S] ") + msg)

    def drain_log(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.txt.configure(state="normal")
                self.txt.insert("end", line + "\n")
                self.txt.see("end")
                self.txt.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self.drain_log)

    def version_check(self, q):
        if not os.path.isfile(q):
            self.log("WARNING: qrenderdoc.exe not found. The bundled RenderDoc 1.25 "
                     "should be in ./tools/RenderDoc_1.25 — or point the path at it.")
            return
        ver = get_file_version(q)
        if ver.startswith("1.25"):
            self.log("RenderDoc %s detected — correct version for this workflow." % ver)
        else:
            self.log("WARNING: qrenderdoc is version %s, but this workflow needs 1.25. "
                     "1.45 crashes Chrome's GPU process on WebGL. Use the bundled "
                     "1.25 in ./tools." % (ver or "unknown"))

    # ---- capture flow

    def start_capture(self):
        if self.busy:
            return
        if not os.path.isfile(self.var_qrd.get()):
            messagebox.showerror(APP_TITLE, "qrenderdoc.exe (RenderDoc 1.25) not found.")
            return
        if not os.path.isfile(self.var_chrome.get()):
            messagebox.showerror(APP_TITLE, "chrome.exe not found.")
            return
        if not os.path.isfile(WORKER):
            messagebox.showerror(APP_TITLE, "capture_worker.py missing next to app.py.")
            return
        self.busy = True
        self.capture_now_event.clear()
        self.btn_go.configure(state="disabled")
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self._capture()
        except Exception as e:
            self.log("ERROR: %r" % e)
        finally:
            self._cleanup_processes()
            self.busy = False
            self.root.after(0, lambda: (self.btn_go.configure(state="normal"),
                                        self.btn_cap.configure(state="disabled")))

    @staticmethod
    def _cleanup_processes():
        """Kill our throwaway Chrome and any orphaned RenderDoc launchers so a
        stale target-control channel or debug port can't jam the next capture."""
        kill_profile_chrome(PROFILE_DIR_NAME)
        for exe in ("qrenderdoc.exe", "renderdoccmd.exe"):
            subprocess.run(["taskkill", "/IM", exe, "/F"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)

    def _capture(self):
        out_dir = self.var_out.get()
        os.makedirs(out_dir, exist_ok=True)
        try:
            lat, lon = geocode(self.var_loc.get())
        except Exception as e:
            self.log("Could not resolve location: %r" % e)
            return
        url = google_earth_url(lat, lon)
        self.log("Location: %.5f, %.5f" % (lat, lon))

        tmp = tempfile.gettempdir()
        cfg_path = os.path.join(tmp, "rdoc_worker_config.json")
        status = os.path.join(tmp, "earth_rdc_status.jsonl")
        fi = os.path.join(tmp, "earth_flag_injected")
        fg = os.path.join(tmp, "earth_flag_go")
        fr = os.path.join(tmp, "earth_flag_result")
        for p in (status, fi, fg, fr):
            try:
                os.remove(p)
            except OSError:
                pass
        open(status, "w").close()
        profile = os.path.join(tmp, PROFILE_DIR_NAME)

        # Start clean: an interrupted previous run can leave an orphaned
        # qrenderdoc/renderdoccmd holding RenderDoc's target-control channel, or a
        # stale Chrome squatting the debug port, which breaks new captures.
        self._cleanup_processes()

        self.log("Launching Chrome... (a 'Google Chrome Gpu' dialog will appear)")
        subprocess.Popen(chrome_cmd(self.var_chrome.get(), profile),
                         env=hooked_env(), creationflags=CREATE_NO_WINDOW)

        pid = None
        for _ in range(50):
            pid = gpu_pid(PROFILE_DIR_NAME)
            if pid:
                break
            time.sleep(0.5)
        if not pid:
            self.log("Could not find Chrome's GPU process. Aborting.")
            return
        self.log("GPU process PID %d found (paused)." % pid)

        json.dump({
            "gpu_pid": pid,
            "capture_file": os.path.join(out_dir, CAPTURE_BASENAME),
            "status_file": status, "flag_injected": fi, "flag_go": fg,
            "flag_result": fr, "go_timeout": 900, "capture_timeout": 90,
        }, open(cfg_path, "w"))

        self.log("Injecting RenderDoc into the GPU process...")
        subprocess.Popen([self.var_qrd.get(), "--python", WORKER])

        # relay worker status to the log
        self._stream_status(status, until_flag=fi, timeout=90)
        if not os.path.exists(fi):
            self.log("Injection did not complete. If a 'Google Chrome Gpu' dialog "
                     "is on screen, DON'T click it yet — the capture needs the "
                     "injection first. Aborting this run.")
            return
        # Injection done — now resume the GPU process by dismissing the dialog.
        if dismiss_gpu_dialog(pid):
            self.log("Injected and hooked; GPU dialog dismissed automatically.")
        else:
            self.log("Injected and hooked. If the 'Google Chrome Gpu' dialog is "
                     "still up, click OK now to continue.")

        time.sleep(2)
        try:
            cdp_navigate(DEBUG_PORT, url)
        except Exception as e:
            self.log("Navigation error: %r" % e)

        # Hand control to the user. The reliable way to get ALL tiles into the
        # captured frame is a REAL camera move (invalidates Google's tile cache
        # so every visible tile's geometry is redrawn). So: let the user frame
        # it, drag the globe, and click "Capture Now" while it's still moving.
        self.log("")
        self.log("=== READY TO CAPTURE ===")
        self.log("In the Chrome window: frame the buildings you want. Then, to "
                 "avoid missing tiles, DRAG the globe a little with your mouse and "
                 "— while it's still gliding — click '2. Capture Now' here.")
        self.root.after(0, lambda: self.btn_cap.configure(state="normal"))
        # Wait for the user's button (up to ~14 min). Check Chrome liveness only
        # every ~5s so we don't spawn a PowerShell process on every loop.
        ticks = 0
        max_ticks = 840 * 5          # 0.2s per tick -> ~14 min
        while not self.capture_now_event.wait(0.2):
            ticks += 1
            if ticks > max_ticks:
                self.log("No capture taken (timed out). Aborting.")
                return
            if ticks % 25 == 0 and not proc_alive(pid):
                self.log("No capture taken (Chrome was closed). Aborting.")
                return
        self.root.after(0, lambda: self.btn_cap.configure(state="disabled"))
        
        self.log("Starting automatic camera orbit...")
        stop_orbit = threading.Event()
        threading.Thread(target=orbit_camera, args=(DEBUG_PORT, stop_orbit), daemon=True).start()

        for i in range(3, 0, -1):
            self.log("Capturing in %d..." % i)
            time.sleep(1)
        self.log("Capturing now...")
        open(fg, "w").close()

        self._stream_status(status, until_flag=fr, timeout=120)
        stop_orbit.set()
        time.sleep(0.3)
        result = ""
        if os.path.exists(fr):
            result = open(fr, encoding="utf-8").read().strip()
        if result and os.path.isfile(result):
            size = os.path.getsize(result) / 1e6
            self.log("")
            self.log("CAPTURE SAVED: %s (%.1f MB)" % (result, size))
            self.log("Import it in Blender 4.1 (with Maps Models Importer v0.7.0): File > Import > Google Maps Capture.")
            self.log("If tiles are still missing: capture again and keep the globe "
                     "MOVING as you click Capture Now (motion forces every tile to "
                     "redraw). A bigger .rdc = more geometry.")
        else:
            self.log("No capture produced. Make sure Chrome is showing the 3D view, "
                     "then try again.")

    def _stream_status(self, status_file, until_flag, timeout):
        end = time.time() + timeout
        seen = 0
        while time.time() < end and not os.path.exists(until_flag):
            try:
                lines = open(status_file, encoding="utf-8").read().splitlines()
            except OSError:
                lines = []
            for ln in lines[seen:]:
                self._log_status(ln)
            seen = len(lines)
            time.sleep(0.4)
        try:
            lines = open(status_file, encoding="utf-8").read().splitlines()
            for ln in lines[seen:]:
                self._log_status(ln)
        except OSError:
            pass

    def _log_status(self, line):
        try:
            rec = json.loads(line)
        except ValueError:
            return
        ev = rec.get("event", "")
        nice = {
            "inject_start": "  - injecting into GPU process",
            "injected": "  - RenderDoc injected (ident %s)" % rec.get("ident"),
            "connected": "  - target control connected",
            "dialog_dismissed": "  - GPU dialog dismissed, process resumed hooked",
            "trigger": "  - capture triggered",
            "new_capture": "  - frame captured",
            "no_capture": "  - no frame captured",
            "inject_failed": "  - INJECT FAILED: %s" % rec.get("detail", ""),
            "connect_failed": "  - target-control connect failed",
            "worker_exception": "  - worker error",
        }.get(ev)
        if nice:
            self.log(nice)

    def open_out_dir(self):
        out = self.var_out.get()
        os.makedirs(out, exist_ok=True)
        os.startfile(out)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
