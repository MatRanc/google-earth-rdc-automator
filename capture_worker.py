"""
Runs INSIDE qrenderdoc's Python:  qrenderdoc.exe --python capture_worker.py

qrenderdoc's embedded Python is stripped down: it has NO `socket`/`ssl`, so this
worker does ONLY what needs the `renderdoc` module, and coordinates with the
outer orchestrator (full Python, which drives Chrome) through small flag files.

Config is read from  %TEMP%\rdoc_worker_config.json  (written by the app).
Sequence:
  1. Inject RenderDoc into the paused Chrome GPU process -> get target ident.
  2. Connect target control; dismiss the --gpu-startup-dialog so the GPU process
     resumes, now hooked. Write <flag_injected>.
  3. Wait for the orchestrator to load the map and write <flag_go>.
  4. Trigger a frame capture, wait for the .rdc, write its path to <flag_result>.

Only uses: renderdoc, os, json, time, ctypes, traceback  (all present).
"""
import os
import json
import time
import ctypes
import traceback

_CANARY = os.path.join(os.environ.get("TEMP", "."), "rdoc_worker_canary.txt")


def _canary(m):
    try:
        with open(_CANARY, "a", encoding="utf-8") as f:
            f.write(str(m) + "\n")
    except Exception:
        pass


_canary("worker start")

_CFG_PATH = os.environ.get("RDOC_WORKER_CONFIG") or os.path.join(
    os.environ.get("TEMP", os.path.expanduser("~")), "rdoc_worker_config.json")
CFG = json.load(open(_CFG_PATH, "r", encoding="utf-8"))
STATUS = CFG["status_file"]
FLAG_INJECTED = CFG["flag_injected"]
FLAG_GO = CFG["flag_go"]
FLAG_RESULT = CFG["flag_result"]
user32 = ctypes.windll.user32


def emit(event, **kw):
    rec = {"event": event, "t": round(time.time(), 2)}
    rec.update(kw)
    with open(STATUS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def dismiss_gpu_dialog(pid, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(hwnd, _):
            # NOTE: do not require IsWindowVisible — the app hides the dialog
            # (SW_HIDE) so the user never sees it, and we must still find it here.
            p = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid:
                buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, buf, 256)
                if buf.value == "#32770":
                    found.append(hwnd)
                    return False
            return True

        user32.EnumWindows(cb, 0)
        if found:
            # Close it without SetForegroundWindow: the app has parked it
            # off-screen, and focusing it would yank it back into view.
            user32.SendMessageW(found[0], 0x0111, 1, 0)  # WM_COMMAND, IDOK
            return True
        time.sleep(0.4)
    return False


def wait_flag(path, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(path):
            return True
        time.sleep(0.25)
    return False


def main():
    import renderdoc as rd

    pid = int(CFG["gpu_pid"])
    template = CFG["capture_file"]

    emit("inject_start", pid=pid)
    opts = rd.GetDefaultCaptureOptions()
    res = rd.InjectIntoProcess(pid, [], template, opts, False)
    ident = res.ident
    if not ident:
        emit("inject_failed", detail=str(getattr(res, "result", "")))
        return
    emit("injected", ident=ident)

    tc = rd.CreateTargetControl("localhost", ident, "bingmaps-auto", True)
    if not tc:
        emit("connect_failed", ident=ident)
        return
    emit("connected", pid=tc.GetPID(), target=tc.GetTarget())

    # Injection done. Tell the app immediately; the APP now owns dismissing the
    # (hidden) GPU dialog so it can guarantee a fallback if anything fails.
    with open(FLAG_INJECTED, "w") as f:
        f.write(str(ident))
    emit("injected_flag_written")

    emit("waiting_for_go")
    if not wait_flag(FLAG_GO, int(CFG.get("go_timeout", 180))):
        emit("go_timeout")
        return

    emit("trigger")
    tc.TriggerCapture(1)
    deadline = time.time() + int(CFG.get("capture_timeout", 60))
    got = None
    while time.time() < deadline:
        msg = tc.ReceiveMessage(None)
        mt = msg.type
        if mt == rd.TargetControlMessageType.NewCapture:
            got = msg.newCapture.path
            emit("new_capture", path=got, bytes=msg.newCapture.byteSize)
            break
        elif mt == rd.TargetControlMessageType.Disconnected:
            emit("disconnected")
            break
        else:
            time.sleep(0.05)
    with open(FLAG_RESULT, "w", encoding="utf-8") as f:
        f.write(got or "")
    if not got:
        emit("no_capture")
    try:
        tc.Shutdown()
    except Exception:
        pass
    emit("done", capture=got)


try:
    _canary("calling main")
    main()
    _canary("main returned")
except Exception:
    _canary("MAIN EXCEPTION:\n" + traceback.format_exc())
    try:
        emit("worker_exception", error=traceback.format_exc())
    except Exception:
        pass
os._exit(0)
