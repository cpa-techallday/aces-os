"""The `aces-console` command (build spec 2).

    aces-console                 same as `open`
    aces-console open            start the server when /api/health does not answer, wait for it, open the page
                                 (omarchy-launch-webapp, else xdg-open, else the Python webbrowser module)
    aces-console start           spawn `python -I -m aces_console serve` detached, wait for health (up to 90 s)
    aces-console serve [--bind H] [--port N] [--static DIR] [--fixtures DIR]      foreground
    aces-console stop            SIGTERM the recorded pid, wait 5 s, SIGKILL, remove the pid file
    aces-console status          pid, url and the /api/health summary, or "not running"
    aces-console url             print the URL
    aces-console <server key>    legacy aces-consoles behaviour: webodm and video_studio open server.<key> from the
                                 aces configuration in a web-app window; grid_control, simlab_console and mlflow are
                                 replaced by this console and open it at the plate that holds their content

Exit codes: 0, 1 failure, 2 usage. The server log is $ACES_STATE/console/server.log; the pid file server.pid.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from . import UI_NAME, __version__, config as console_config, httpc

USAGE = """usage: aces-console [open|start|serve|stop|status|url] [options]
       aces-console <server key>      (legacy: webodm | video_studio; grid_control | simlab_console | mlflow open this console)

  serve  --bind H  --port N  --static DIR  --fixtures DIR   (fixtures only with ACES_CONSOLE_ALLOW_FIXTURES=1)
"""
COMMANDS = ("open", "start", "serve", "stop", "status", "url")
LEGACY_KEYS = ("webodm", "video_studio")
# GRID CONTROL, the SIMLAB console and MLflow are replaced by the ACES console (Aaron, 2026-10-11): their old keys open
# the console at the plate that holds their content now
REPLACED_KEYS = {"grid_control": "ops", "simlab_console": "pipeline", "mlflow": "pipeline"}
START_WAIT_S = 90.0  # 45 x 2 s, the aces-webodm readiness ceiling (src/aces-survey/aces-webodm:124-131)
STOP_WAIT_S = 5.0


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _notify(msg: str) -> None:
    exe = shutil.which("notify-send")
    if exe:
        try:
            subprocess.run([exe, "ACES", msg], check=False, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


def _settings(argv: list[str] | None = None) -> console_config.Settings:
    ap = argparse.ArgumentParser(prog="aces-console serve", add_help=False)
    ap.add_argument("--bind", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--static", default=None)
    ap.add_argument("--fixtures", default=None)
    ns, _unknown = ap.parse_known_args(argv or [])
    return console_config.load(bind=ns.bind, port=ns.port, static=ns.static, fixtures=ns.fixtures)


def health(settings: console_config.Settings, timeout: float = 2.0) -> dict | None:
    """The /api/health data of a running server, or None."""
    status, obj, err = httpc.get_json(settings.url + "/api/health", timeout)
    if err or not isinstance(obj, dict) or not obj.get("ok"):
        return None
    data = obj.get("data")
    return data if isinstance(data, dict) else None


def _read_pid(settings: console_config.Settings) -> int | None:
    try:
        text = settings.pid_file.read_text(encoding="utf-8").strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def open_in_browser(url: str) -> bool:
    """omarchy-launch-webapp when present, else xdg-open, else the webbrowser module (Windows development)."""
    for exe in ("omarchy-launch-webapp", "xdg-open"):
        path = shutil.which(exe)
        if path:
            try:
                subprocess.Popen([path, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 close_fds=True)
                return True
            except OSError as e:
                _err(f"aces-console: {exe} failed: {e}")
    return bool(webbrowser.open(url))


def _tail(path: Path, n: int = 12) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def server_command(args: list[str]) -> list[str]:
    """The detached server's command line: `python -I -m aces_console serve ...` for the installed package. From a
    source tree (development) `python -I` ignores PYTHONPATH, so the same program runs through `-c` with this tree
    and the `aces` package's location put on sys.path explicitly; the server is still that python process."""
    import json

    here = Path(__file__).resolve().parent.parent
    if any(part in ("site-packages", "dist-packages") for part in here.parts):
        return [sys.executable, "-I", "-m", "aces_console", "serve", *args]
    paths = [str(here)]
    try:
        import aces

        paths.append(str(Path(aces.__file__).resolve().parent.parent))
    except ImportError:
        pass
    prog = (f"import sys; sys.path[:0] = {json.dumps(paths)}; from aces_console.cli import main; "
            f"raise SystemExit(main({json.dumps(['serve', *args])}))")
    return [sys.executable, "-I", "-c", prog]


# --- commands ---------------------------------------------------------------------------------------------------------------

def cmd_serve(argv: list[str]) -> int:
    from . import server

    ap = argparse.ArgumentParser(prog="aces-console serve", description="run the ACES console server in the foreground")
    ap.add_argument("--bind", default=None, help="loopback address (127.0.0.1 or ::1)")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--static", default=None, help="static tree (development)")
    ap.add_argument("--fixtures", default=None, help="replay captured JSON instead of calling upstreams (tests)")
    ns = ap.parse_args(argv)
    try:
        settings = console_config.load(bind=ns.bind, port=ns.port, static=ns.static, fixtures=ns.fixtures)
    except SystemExit as e:
        if e.code not in (None, 0):
            _err(str(e.code))
            return 2
        raise
    try:
        return server.serve(settings)
    except SystemExit as e:
        if isinstance(e.code, str):
            _err(e.code)
            _notify(e.code)
            return 1
        return int(e.code or 0)


def cmd_start(argv: list[str]) -> int:
    try:
        settings = _settings(argv)
    except SystemExit as e:
        _err(str(e.code))
        return 2
    running = health(settings)
    if running is not None:
        print(f"already running: pid {running.get('pid')} at {settings.url}")
        return 0
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(settings.log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    cmd = server_command(list(argv))
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": fd, "stderr": fd, "close_fds": True,
                    "cwd": str(settings.state_dir), "env": dict(os.environ)}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as e:
        os.close(fd)
        msg = f"aces-console: cannot start the server: {e}"
        _err(msg)
        _notify(msg)
        return 1
    os.close(fd)
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            msg = f"aces-console: the server exited with status {proc.returncode} before answering; see {settings.log_file}"
            _err(msg)
            tail = _tail(settings.log_file)
            if tail:
                _err(tail)
            _notify(msg)
            return 1
        data = health(settings, timeout=1.0)
        if data is not None:
            print(f"started: pid {data.get('pid')} at {settings.url}")
            if not settings.pid_file.is_file():
                settings.pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
            proc.returncode = 0  # detached on purpose: this process never waits for the server
            return 0
        time.sleep(0.2)
    msg = f"aces-console: the server did not answer within {START_WAIT_S:.0f} s; see {settings.log_file}"
    _err(msg)
    _notify(msg)
    return 1


def cmd_stop(argv: list[str]) -> int:
    try:
        settings = _settings(argv)
    except SystemExit as e:
        _err(str(e.code))
        return 2
    pid = _read_pid(settings)
    data = health(settings)
    if data is not None and isinstance(data.get("pid"), int):
        pid = data["pid"]
    if pid is None or not _alive(pid):
        if settings.pid_file.is_file():
            settings.pid_file.unlink(missing_ok=True)
        print("not running")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        _err(f"aces-console: cannot signal pid {pid}: {e}")
        return 1
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.1)
    if _alive(pid):
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass
        time.sleep(0.2)
    settings.pid_file.unlink(missing_ok=True)
    print(f"stopped pid {pid}")
    return 0


def cmd_status(argv: list[str]) -> int:
    try:
        settings = _settings(argv)
    except SystemExit as e:
        _err(str(e.code))
        return 2
    pid = _read_pid(settings)
    data = health(settings)
    if data is None:
        print(f"not running ({settings.url}{'; stale pid file ' + str(settings.pid_file) if pid else ''})")
        return 1
    mods = ", ".join(f"{k}={v}" for k, v in (data.get("modules") or {}).items())
    print(f"running: pid {data.get('pid')} at {settings.url} (version {data.get('version')}, clients {data.get('clients')}, "
          f"poller {data.get('poller')}, started {data.get('started_utc')}{'; ' + data['fixture'] if data.get('fixture') else ''})")
    print(f"modules: {mods}")
    print(f"state: {settings.state_dir}")
    return 0


def cmd_url(argv: list[str]) -> int:
    try:
        settings = _settings(argv)
    except SystemExit as e:
        _err(str(e.code))
        return 2
    print(settings.url)
    return 0


def cmd_open(argv: list[str], plate: str | None = None) -> int:
    try:
        settings = _settings(argv)
    except SystemExit as e:
        _err(str(e.code))
        return 2
    if health(settings) is None:
        rc = cmd_start(argv)
        if rc != 0:
            return rc
    url = settings.url + (f"?plate={plate}" if plate else "")
    return 0 if open_in_browser(url) else 1


def cmd_legacy(key: str) -> int:
    """The aces-consoles shim (src/aces-consoles/aces-console:11-21): open server.<key> or fail honestly. GRID CONTROL,
    the SIMLAB console and MLflow are replaced: their keys open this console at the plate holding their content."""
    if key in REPLACED_KEYS:
        print(f"aces-console: {key} is replaced by the ACES console; opening its {REPLACED_KEYS[key]} plate", file=sys.stderr)
        return cmd_open([], plate=REPLACED_KEYS[key])
    from aces import config as aces_config

    try:
        url = (aces_config.load().get("server") or {}).get(key)
    except SystemExit as e:
        _err(str(e.code))
        return 1
    if not isinstance(url, str) or not url:
        msg = f"aces-console: no URL configured for server.{key}; set it in ~/.config/aces/aces.toml"
        _err(msg)
        _notify(msg)
        return 1
    return 0 if open_in_browser(url) else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "open"
    rest = argv[1:]
    if cmd in ("-h", "--help", "help"):
        print(USAGE.rstrip())
        return 2
    if cmd in ("-V", "--version"):
        print(UI_NAME)
        return 0
    if cmd == "serve":
        return cmd_serve(rest)
    if cmd in COMMANDS:
        return {"open": cmd_open, "start": cmd_start, "stop": cmd_stop, "status": cmd_status, "url": cmd_url}[cmd](rest)
    if cmd.startswith("-") or rest:
        _err(USAGE.rstrip())
        return 2
    return cmd_legacy(cmd)
