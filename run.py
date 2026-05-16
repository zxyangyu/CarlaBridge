"""Cross-platform launcher for CarlaBridge.

Replaces the old ``run.ps1``. Uses only Python stdlib so it can be invoked
with any reasonably recent Python (the embedded ``.venv\\python.exe`` is
preferred, but ``py -3`` / system python work too — they just re-exec the
bridge under the venv interpreter).

Pre-launch port handling
------------------------
If something is already listening on the bridge port (default ``5000``),
the launcher does NOT kill the process. It POSTs ``/admin/shutdown`` —
the same graceful path used by ``scripts/restart_smoke.ps1`` — and polls
the port until it is fully released. This avoids leaving CARLA in
``synchronous_mode=True`` (see README §6.3).

If the port is in TIME_WAIT (recently closed listener, no live process),
we just warn — there is nothing to shut down, the OS releases it itself.

Usage
-----
    python run.py
    python run.py --scenario s1_fire --log-level DEBUG
    python run.py --no-carla
    python run.py --port 5001                # match a non-default server.port
    python run.py --no-port-release          # skip pre-flight (legacy behavior)
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
VENV_PYTHON = REPO_ROOT / ".venv" / ("python.exe" if os.name == "nt" else "bin/python")
DEFAULT_PORT = 5000
SHUTDOWN_TIMEOUT_S = 45  # mirrors restart_smoke.ps1; CARLA teardown can be slow


def _is_port_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    """True if a TCP connect to (host, port) succeeds — i.e. someone is accepting."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


def _has_time_wait(port: int) -> bool:
    """Best-effort TIME_WAIT detection — returns False on any error."""
    try:
        import psutil  # type: ignore
    except ImportError:
        return False
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == port and c.status == "TIME_WAIT":
                return True
    except (psutil.AccessDenied, OSError):
        return False
    return False


def _post_shutdown(port: int, timeout: float = 5.0) -> tuple[bool, str]:
    """POST /admin/shutdown. Returns (ok, message)."""
    url = f"http://127.0.0.1:{port}/admin/shutdown"
    req = urllib.request.Request(url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return True, body.strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return False, f"HTTP {e.code}: {body.strip()}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"connect failed: {e}"


def _wait_port_free(port: int, deadline_s: float, poll_interval: float = 1.0) -> bool:
    """Poll until the listener on `port` goes silent or `deadline_s` elapses."""
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if not _is_port_listening("127.0.0.1", port):
            return True
        time.sleep(poll_interval)
    return False


def release_port(port: int) -> bool:
    """Pre-flight: gracefully shut down any bridge currently bound to `port`.

    Returns True if the port is free (or was never taken). Returns False if
    something is still holding the port after the shutdown timeout — caller
    should refuse to launch in that case.
    """
    listening = _is_port_listening("127.0.0.1", port)
    if not listening:
        if _has_time_wait(port):
            print(
                f"==> warning: port {port} in TIME_WAIT, will clear in ~60s",
                flush=True,
            )
            print("    if launch fails with errno 10048, wait and retry", flush=True)
        return True

    print(
        f"==> port {port} is in use — posting /admin/shutdown to release gracefully",
        flush=True,
    )
    ok, msg = _post_shutdown(port)
    if not ok:
        print(f"    /admin/shutdown failed: {msg}", file=sys.stderr, flush=True)
        print(
            "    refusing to launch; investigate manually (the holder may not be a "
            "bridge — check `Get-NetTCPConnection -LocalPort {0}`).".format(port),
            file=sys.stderr,
            flush=True,
        )
        return False

    print(f"    server replied: {msg}", flush=True)
    print(
        f"    waiting up to {SHUTDOWN_TIMEOUT_S}s for port {port} to release...",
        flush=True,
    )
    if _wait_port_free(port, SHUTDOWN_TIMEOUT_S):
        print(f"    port {port} released", flush=True)
        return True

    print(
        f"    timed out after {SHUTDOWN_TIMEOUT_S}s — bridge did not exit cleanly. "
        "Refusing to launch (force-killing risks leaving CARLA in sync mode; see "
        "README §6.3).",
        file=sys.stderr,
        flush=True,
    )
    return False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="Launch CarlaBridge with pre-flight graceful port release.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python run.py\n"
            "  python run.py --scenario s1_fire --log-level DEBUG\n"
            "  python run.py --no-carla\n"
        ),
    )
    p.add_argument("--scenario", help="override scenario.default (e.g. s1_fire)")
    p.add_argument("--config", help="extra TOML overlay merged on top of default+local")
    p.add_argument("--log-level", help="DEBUG / INFO / WARN / ERROR")
    p.add_argument(
        "--no-carla",
        action="store_true",
        help="skip CARLA connection (HTTP + Socket.IO only; for frontend smoke)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"bridge port to release before launch (default {DEFAULT_PORT}); "
        "must match server.port in your config",
    )
    p.add_argument(
        "--no-port-release",
        action="store_true",
        help="skip the graceful pre-flight shutdown of the existing bridge",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not VENV_PYTHON.exists():
        print(
            f"venv python not found at {VENV_PYTHON}\n"
            "The bridge is pinned to this exact path (CARLA's wheel only installs into\n"
            "a Py 3.12 env and we've registered all deps there). If you've moved your\n"
            "env, update VENV_PYTHON in this script.",
            file=sys.stderr,
        )
        return 1

    if not args.no_port_release:
        if not release_port(args.port):
            return 2

    cmd: list[str] = [str(VENV_PYTHON), "-m", "carlabridge.main"]
    if args.scenario:
        cmd += ["--scenario", args.scenario]
    if args.config:
        cmd += ["--config", args.config]
    if args.log_level:
        cmd += ["--log-level", args.log_level]
    if args.no_carla:
        cmd += ["--no-carla"]

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"

    print(f"==> launching: {' '.join(cmd)}", flush=True)
    print("==> URLs:", flush=True)
    print(f"    healthz : http://localhost:{args.port}/healthz")
    print(f"    events  : http://localhost:{args.port}/debug/events?n=200")
    print(f"    mjpeg   : http://localhost:{args.port}/video_feed?camera=city|aerial|ground")
    print(f"    webrtc  : POST http://localhost:{args.port}/webrtc/{{camera}}")
    print()

    try:
        return subprocess.call(cmd, cwd=str(REPO_ROOT), env=env)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
