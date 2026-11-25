# updater.py - keeps two things up to date from a PRIVATE Google Drive folder:
#   1) the Chrome extension folder (silently; the extension then reloads itself)
#   2) the tool's own exe (asks the user first, then restarts)
#
# The main program only uses two things from this file:
#   start(...)                 - called once when the tool starts
#   local_ext_version(ext_dir) - used by the local /ext-version web address
import os
import sys
import json
import shutil
import zipfile
import tempfile
import subprocess
import threading
import time
import queue

import pgs_store

CHECK_EVERY_SECONDS = 600            # look for updates every 10 minutes
VERSION_FILE_NAME = "version.json"   # the 3 file names you upload to the Drive folder
ZIP_FILE_NAME = "extension.zip"
EXE_FILE_NAME = "PGS_Tool.exe"

NEW_EXE_NAME = "PGS_Tool_new.exe"          # where the downloaded exe is stored first
UPDATE_SCRIPT_NAME = "finish_update.bat"   # helper script that swaps the exe after the tool closes
UPDATE_LOG_NAME = "pgs_update_helper.log"   # tiny temp log written by finish_update.bat; moved into settings.dat at the next start

CREATE_NO_WINDOW = 0x08000000        # Windows flag: start a helper without a black console window

_declined = set()                    # versions the user said "not now" to (this session only)
_ui_queue = queue.Queue()                # Tk calls must run on the main thread: the background thread puts work here


# ---------------- small helpers ----------------
def _ver(v):
    """'1.2.6' -> (1, 2, 6) so versions can be compared with > and <."""
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except Exception:
        return (0,)


def local_ext_version(ext_dir):
    """Version written inside the extension folder's manifest.json ('' if there is no extension yet)."""
    try:
        with open(os.path.join(ext_dir, "manifest.json"), encoding="utf-8-sig") as f:
            return str(json.load(f).get("version", ""))
    except Exception:
        return ""


def _helper_log_path():
    return os.path.join(tempfile.gettempdir(), UPDATE_LOG_NAME)


def _write_log(script_dir, text):
    """Adds one line to the update log inside settings.dat. Never raises."""
    pgs_store.append_log("update", text)


def _absorb_helper_log():
    """finish_update.bat cannot write into settings.dat, so it writes a temp file; copy it in and delete it."""
    p = _helper_log_path()
    try:
        if os.path.isfile(p):
            with open(p, "r", encoding="mbcs" if sys.platform == "win32" else "utf-8", errors="replace") as f:
                text = f.read().strip()
            if text:
                pgs_store.append_log("update", "[helper] " + text)
            os.remove(p)
    except Exception:
        pass


# ---------------- Google Drive (read only, through the same Google key the tool uses) ----------------
def _have_credentials(c):
    """c is either the key itself (a dict, built into the exe) or the path of a credentials.json file."""
    return isinstance(c, dict) or bool(c and os.path.exists(c))


def _session(credentials):
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    if isinstance(credentials, dict):
        creds = Credentials.from_service_account_info(credentials, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(credentials, scopes=scopes)
    return AuthorizedSession(creds)


def _find_file(sess, folder_id, name):
    """Newest file with this name inside the folder (so just uploading again is enough)."""
    r = sess.get("https://www.googleapis.com/drive/v3/files", params={
        "q": f"'{folder_id}' in parents and name = '{name}' and trashed = false",
        "orderBy": "modifiedTime desc", "pageSize": 1, "fields": "files(id,name)",
        "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, timeout=30)
    r.raise_for_status()
    files = r.json().get("files", [])
    return files[0]["id"] if files else None


def _read_json(sess, file_id):
    r = sess.get(f"https://www.googleapis.com/drive/v3/files/{file_id}",
                 params={"alt": "media", "supportsAllDrives": "true"}, timeout=30)
    r.raise_for_status()
    return json.loads(r.content.decode("utf-8-sig"))


def _download(sess, file_id, dest):
    """Downloads into a temporary file first, so a failed download never leaves a broken file behind."""
    part = dest + ".part"
    with sess.get(f"https://www.googleapis.com/drive/v3/files/{file_id}",
                  params={"alt": "media", "supportsAllDrives": "true"}, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(part, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                if chunk:
                    f.write(chunk)
    os.replace(part, dest)


def _is_valid_exe(path):
    """A real Windows exe starts with the letters 'MZ' and is at least a few MB big."""
    try:
        if os.path.getsize(path) < 1024 * 1024:
            return False
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except Exception:
        return False


# ---------------- small state file (stops endless update loops) ----------------
def _load_state(path=None):
    state = pgs_store.get("update_state", {})
    return state if isinstance(state, dict) else {}


def _save_state(path, state):
    try:
        pgs_store.set("update_state", state)
    except Exception:
        pass


# ---------------- extension (silent) ----------------
def install_extension(sess, folder_id, ext_dir, log):
    zid = _find_file(sess, folder_id, ZIP_FILE_NAME)
    if not zid:
        log(f"[update] {ZIP_FILE_NAME} was not found in the Drive folder")
        return False
    tmp = tempfile.mkdtemp(prefix="pgs_ext_")
    try:
        zpath = os.path.join(tmp, "ext.zip")
        _download(sess, zid, zpath)
        out = os.path.join(tmp, "x")
        with zipfile.ZipFile(zpath) as z:
            z.extractall(out)
        src = None
        for folder, _dirs, files in os.walk(out):      # works with or without a folder inside the zip
            if "manifest.json" in files:
                src = folder
                break
        if not src:
            log("[update] no manifest.json inside extension.zip")
            return False
        os.makedirs(ext_dir, exist_ok=True)
        shutil.copytree(src, ext_dir, dirs_exist_ok=True)   # overwrite files in place
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- exe update ----------------
def _write_update_script(exe, new_exe, log_path):
    """
    Writes a small Windows batch file that:
      1. waits until THIS program has fully closed (so its temp files are gone),
      2. moves the new exe over the old one (retries if antivirus is still holding the file),
      3. starts the new exe,
      4. deletes itself.
    Every step is written to a small temp log that the tool moves into settings.dat.
    """
    bat = os.path.join(os.path.dirname(exe), UPDATE_SCRIPT_NAME)
    pid = os.getpid()
    lines = [
        "@echo off",
        f'set "LOG={log_path}"',
        f'echo [%date% %time%] waiting for the old program (pid {pid}) to close >> "%LOG%"',
        "set /a TRIES=0",
        ":waitloop",
        f'tasklist /FI "PID eq {pid}" 2>NUL | find " {pid} " >NUL',
        "if errorlevel 1 goto closed",
        "set /a TRIES+=1",
        "if %TRIES% GEQ 60 goto giveup",
        "ping -n 2 127.0.0.1 >NUL",
        "goto waitloop",
        ":closed",
        'echo [%date% %time%] old program closed >> "%LOG%"',
        "set /a MOVES=0",
        ":moveloop",
        f'move /Y "{new_exe}" "{exe}" >NUL 2>> "%LOG%"',
        "if not errorlevel 1 goto moved",
        "set /a MOVES+=1",
        "if %MOVES% GEQ 20 goto giveup",
        "ping -n 2 127.0.0.1 >NUL",
        "goto moveloop",
        ":moved",
        'echo [%date% %time%] new version is in place, starting it >> "%LOG%"',
        f'start "" "{exe}"',
        'del "%~f0"',
        "exit /b 0",
        ":giveup",
        'echo [%date% %time%] update stopped - the old exe was kept >> "%LOG%"',
        "exit /b 1",
    ]
    # Windows batch files use the ANSI code page ("mbcs" on Windows)
    with open(bat, "w", encoding="mbcs", errors="replace") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return bat


def _apply_exe_update(new_exe, script_dir):
    """Starts the helper and returns. The caller must close the program right after this."""
    exe = sys.executable
    bat = _write_update_script(exe, new_exe, _helper_log_path())
    subprocess.Popen(["cmd", "/c", bat], creationflags=CREATE_NO_WINDOW,
                     cwd=os.path.dirname(exe), close_fds=True)


def _ask_and_apply(root, quit_app, state_path, script_dir, r_app, new_exe, log):
    from tkinter import messagebox
    if not messagebox.askyesno("Update", f"A new version ({r_app}) of the tool is ready.\n\n"
                                         "Update now? The tool will restart."):
        _declined.add(r_app)
        _write_log(script_dir, f"user postponed update to {r_app}")
        return
    try:
        state = _load_state(state_path)
        state["exe_tried"] = r_app
        _save_state(state_path, state)
        _write_log(script_dir, f"starting update to {r_app}")
        _apply_exe_update(new_exe, script_dir)
    except Exception as e:
        _write_log(script_dir, f"could not start update: {type(e).__name__}: {e}")
        log(f"[update] could not start the update: {type(e).__name__}: {e}")
        messagebox.showerror("Update", f"Update failed:\n{e}")
        return
    log(f"[update] restarting to version {r_app}")
    quit_app()


# ---------------- the check ----------------
def check_once(app_version, script_dir, credentials_file, folder_id, log, root, quit_app):
    if not folder_id or folder_id.startswith("PASTE") or not _have_credentials(credentials_file):
        return
    sess = _session(credentials_file)
    vid = _find_file(sess, folder_id, VERSION_FILE_NAME)
    if not vid:
        log(f"[update] {VERSION_FILE_NAME} was not found in the Drive folder")
        return
    remote = _read_json(sess, vid)
    ext_dir = os.path.join(script_dir, "extension")
    state_path = None   # the state now lives in settings.dat (pgs_store)
    state = _load_state(state_path)

    # 1) extension: silent
    r_ext = str(remote.get("extension", ""))
    mine = local_ext_version(ext_dir)
    if r_ext and (mine == "" or (mine != r_ext and state.get("ext_installed_for") != r_ext)):
        if install_extension(sess, folder_id, ext_dir, log):
            state["ext_installed_for"] = r_ext
            _save_state(state_path, state)
            now = local_ext_version(ext_dir)
            log(f"[update] extension files updated (manifest version {now})")
            if now != r_ext:
                log(f"[update] WARNING: version.json says {r_ext} but manifest.json says {now} - "
                    "change the version in manifest.json too")

    # 2) exe: only when running as an exe, and only after asking
    r_app = str(remote.get("app", ""))
    if not (getattr(sys, "frozen", False) and r_app and _ver(r_app) > _ver(app_version)
            and r_app not in _declined):
        return
    if state.get("exe_tried") == r_app:
        log(f"[update] version.json says {r_app} but this exe still says {app_version} - "
            "did you forget to change APP_VERSION before building?")
        return
    eid = _find_file(sess, folder_id, EXE_FILE_NAME)
    if not eid:
        log(f"[update] {EXE_FILE_NAME} was not found in the Drive folder")
        return
    new_exe = os.path.join(script_dir, NEW_EXE_NAME)
    _download(sess, eid, new_exe)
    if not _is_valid_exe(new_exe):
        _write_log(script_dir, f"downloaded file for {r_app} is not a valid exe - skipped")
        log("[update] downloaded exe looks broken - update skipped")
        try:
            os.remove(new_exe)
        except Exception:
            pass
        return
    _ui_queue.put(lambda: _ask_and_apply(root, quit_app, state_path, script_dir, r_app, new_exe, log))


def start(app_version, script_dir, credentials_file, folder_id, log, root, quit_app):
    # clean up leftovers from earlier updates
    leftovers = [os.path.join(script_dir, NEW_EXE_NAME), os.path.join(script_dir, UPDATE_SCRIPT_NAME)]
    if getattr(sys, "frozen", False):
        leftovers.append(sys.executable + ".old")          # made by older updater versions
    for p in leftovers:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    _absorb_helper_log()

    def loop():
        time.sleep(3)
        while True:
            try:
                check_once(app_version, script_dir, credentials_file, folder_id, log, root, quit_app)
            except Exception as e:
                log(f"[update] check failed: {type(e).__name__}: {e}")
            time.sleep(CHECK_EVERY_SECONDS)

    def pump():

        try:

            while True:

                _ui_queue.get_nowait()()

        except queue.Empty:

            pass

        except Exception:

            pass

        try:

            root.after(500, pump)

        except Exception:

            pass

    root.after(500, pump)

    threading.Thread(target=loop, daemon=True).start()
