"""Desktop hybrid launcher for Printstack (single-instance, zero console window)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

if not getattr(sys, "frozen", False):
    SRC_DIR = Path(__file__).resolve().parent.parent
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))

from printstack.config import (  # noqa: E402
    BROWSER_PROFILE_DIR,
    DATA_DIR,
    SERVER_HOST,
    SERVER_INFO_PATH,
    SERVER_PORT,
    ensure_dirs,
)

ensure_dirs()

# Guard stdio when launched via pythonw.exe or windowed PyInstaller exe
LOG_PATH = DATA_DIR / "app.log"
if sys.stdout is None or sys.stderr is None:
    log_file = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = log_file
    if sys.stderr is None:
        sys.stderr = log_file

import uvicorn  # noqa: E402
import win32api  # noqa: E402
import win32event  # noqa: E402

from printstack.main import app  # noqa: E402
from printstack.printer_service import find_app_window_hwnd  # noqa: E402


def _http_json(base_url: str, method: str, path: str, body: Optional[dict] = None, timeout: float = 2.5):
    url = f"{base_url}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _is_server_healthy(base_url: str) -> bool:
    try:
        res = _http_json(base_url, "GET", "/api/health", timeout=1.0)
        return res.get("status") == "ok" and "word_engine" in res
    except Exception:
        return False


def _read_running_server_url() -> Optional[str]:
    candidates: list[str] = []
    if SERVER_INFO_PATH.exists():
        try:
            info = json.loads(SERVER_INFO_PATH.read_text(encoding="utf-8"))
            host = info.get("host") or SERVER_HOST
            port = int(info.get("port") or 0)
            if port > 0:
                candidates.append(f"http://{host}:{port}")
        except Exception:
            pass
    default_url = f"http://{SERVER_HOST}:{SERVER_PORT}"
    if default_url not in candidates:
        candidates.append(default_url)

    for url in candidates:
        if _is_server_healthy(url):
            return url
    return None


def _pick_available_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((SERVER_HOST, preferred))
            return preferred
        except OSError:
            pass

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((SERVER_HOST, 0))
        return int(s.getsockname()[1])


def _write_server_info(port: int) -> None:
    try:
        SERVER_INFO_PATH.write_text(
            json.dumps({"host": SERVER_HOST, "port": port, "pid": os.getpid()}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _clear_server_info() -> None:
    try:
        SERVER_INFO_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def _find_app_browser() -> Optional[Path]:
    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / r"Microsoft\Edge\Application\msedge.exe",
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / r"Google\Chrome\Application\chrome.exe",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def main() -> None:
    cli_paths = [str(Path(a).resolve()) for a in sys.argv[1:] if a and Path(a).exists()]

    mutex = win32event.CreateMutex(None, False, "Local\\PrintstackStartupMutex")
    win32event.WaitForSingleObject(mutex, 15000)

    server = None
    server_thread = None
    base_url = f"http://{SERVER_HOST}:{SERVER_PORT}"

    try:
        existing_url = _read_running_server_url()
        if existing_url:
            if cli_paths:
                try:
                    _http_json(existing_url, "POST", "/api/queue/import-paths", {"paths": cli_paths}, timeout=10.0)
                except Exception:
                    pass
            focused = False
            try:
                res = _http_json(existing_url, "POST", "/api/window/focus", {}, timeout=2.0)
                focused = bool(res.get("focused"))
            except Exception:
                pass
            if not focused:
                browser = _find_app_browser()
                if browser is not None:
                    subprocess.Popen([
                        str(browser),
                        f"--app={existing_url}",
                        f"--user-data-dir={BROWSER_PROFILE_DIR}",
                        "--window-size=1360,860",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ])
                else:
                    import webbrowser

                    webbrowser.open(existing_url)
            return

        port = _pick_available_port(SERVER_PORT)
        base_url = f"http://{SERVER_HOST}:{port}"

        config = uvicorn.Config(
            app=app,
            host=SERVER_HOST,
            port=port,
            log_level="warning",
            log_config=None,
        )
        server = uvicorn.Server(config)
        server_thread = threading.Thread(target=server.run, name="UvicornServer", daemon=True)
        server_thread.start()

        deadline = time.monotonic() + 15.0
        ready = False
        while time.monotonic() < deadline and server_thread.is_alive():
            if _is_server_healthy(base_url):
                ready = True
                break
            time.sleep(0.1)

        if not ready:
            return

        _write_server_info(port)

        if cli_paths:
            try:
                _http_json(base_url, "POST", "/api/queue/import-paths", {"paths": cli_paths}, timeout=15.0)
            except Exception:
                pass
    finally:
        try:
            win32event.ReleaseMutex(mutex)
            win32api.CloseHandle(mutex)
        except Exception:
            pass

    try:
        browser = _find_app_browser()
        if browser is not None:
            cmd = [
                str(browser),
                f"--app={base_url}",
                f"--user-data-dir={BROWSER_PROFILE_DIR}",
                "--window-size=1360,860",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            start_ts = time.monotonic()
            proc = subprocess.Popen(cmd)
            proc.wait()

            # If browser handed off to an existing profile process immediately,
            # keep server alive while either the app window exists OR the UI WebSocket is connected.
            if time.monotonic() - start_ts < 2.5:
                wait_open = time.monotonic() + 12.0
                while time.monotonic() < wait_open:
                    with app.state.ctx._ws_lock:
                        has_ws = len(app.state.ctx.ws_clients) > 0
                    if has_ws or find_app_window_hwnd("Printstack"):
                        break
                    time.sleep(0.25)

                grace_lost_since: Optional[float] = None
                while True:
                    with app.state.ctx._ws_lock:
                        has_ws = len(app.state.ctx.ws_clients) > 0
                    has_hwnd = bool(find_app_window_hwnd("Printstack"))
                    if has_ws or has_hwnd:
                        grace_lost_since = None
                    else:
                        if grace_lost_since is None:
                            grace_lost_since = time.monotonic()
                        elif time.monotonic() - grace_lost_since >= 3.0:
                            break
                    time.sleep(0.5)
        else:
            import webbrowser

            webbrowser.open(base_url)
            while server_thread and server_thread.is_alive():
                time.sleep(1.0)
    finally:
        _clear_server_info()
        if server is not None:
            server.should_exit = True
        if server_thread is not None:
            server_thread.join(timeout=5.0)


if __name__ == "__main__":
    main()
