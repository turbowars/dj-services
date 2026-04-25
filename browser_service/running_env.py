"""Service registry and port management backed by ~/.running_env + ~/.local-services."""
from __future__ import annotations

import atexit
import fcntl
import logging
import os
import shlex
import signal
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

import httpx

log = logging.getLogger("browser_service.running_env")

RUNNING_ENV = Path.home() / ".running_env"
LOCAL_SERVICES = Path.home() / ".local-services"


@contextmanager
def _locked(path: Path):
    # File lock guards concurrent writers from multiple local processes.
    path.touch(exist_ok=True)
    with open(path, "r+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield fh
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _upsert(path: Path, key: str, value: str) -> None:
    with _locked(path) as fh:
        fh.seek(0)
        kept = [ln for ln in fh.read().splitlines() if ln and not ln.startswith(f"{key}=")]
        kept.append(f"{key}={value}")
        fh.seek(0)
        fh.truncate()
        fh.write("\n".join(kept) + "\n")


def _remove(path: Path, key: str) -> None:
    if not path.exists():
        return
    with _locked(path) as fh:
        fh.seek(0)
        kept = [ln for ln in fh.read().splitlines() if ln and not ln.startswith(f"{key}=")]
        fh.seek(0)
        fh.truncate()
        if kept:
            fh.write("\n".join(kept) + "\n")


def _read(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    with _locked(path) as fh:
        fh.seek(0)
        for ln in fh.read().splitlines():
            if ln.startswith(f"{key}="):
                return ln.split("=", 1)[1]
    return None


def register_service(key: str, host: str, port: int, run_cmd: str | None = None) -> str:
    """Write the endpoint to ~/.running_env and persist the run command to ~/.local-services."""
    endpoint = f"http://{host}:{port}"
    _upsert(RUNNING_ENV, key, endpoint)
    if run_cmd:
        _upsert(LOCAL_SERVICES, key, run_cmd)
    atexit.register(lambda: deregister_service(key))
    log.info("registered %s -> %s", key, endpoint)
    return endpoint


def deregister_service(key: str) -> None:
    _remove(RUNNING_ENV, key)
    log.info("deregistered %s", key)


def read_service(key: str) -> str | None:
    return _read(RUNNING_ENV, key)


def resolve_port(env_var: str, service_key: str, default: int) -> int:
    # Resolution order: explicit env var -> previously registered endpoint -> default.
    raw = os.environ.get(env_var)
    if raw and raw.isdigit():
        return int(raw)
    endpoint = read_service(service_key)
    if endpoint:
        try:
            return int(endpoint.rsplit(":", 1)[1].split("/")[0])
        except (ValueError, IndexError):
            pass
    return default


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) != 0


def _pids_on_port(port: int) -> list[int]:
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [int(p) for p in out.split() if p.strip().isdigit()]


def _process_command(pid: int) -> str:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _kill(pid: int, grace: float = 5.0) -> None:
    # Graceful SIGTERM first, then SIGKILL as last resort.
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + grace
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def acquire_port(preferred: int, process_signatures: Iterable[str], service_key: str) -> int:
    """Return a usable port, killing our own previous instance if needed."""
    sigs = [s for s in process_signatures if s]

    if _port_free(preferred):
        return preferred

    pids = _pids_on_port(preferred)
    for pid in pids:
        cmd = _process_command(pid)
        # Only reclaim the port from known matching commands, not arbitrary processes.
        if any(sig in cmd for sig in sigs):
            log.info("reclaiming port %d from our previous pid %d", preferred, pid)
            _kill(pid)
    if _port_free(preferred):
        return preferred

    for candidate in range(preferred + 1, preferred + 100):
        if _port_free(candidate):
            log.warning("port %d taken, falling back to %d for %s", preferred, candidate, service_key)
            return candidate
    raise RuntimeError(f"no free port near {preferred}")


def ensure_dependency(service_key: str, health_path: str = "/health", timeout: float = 30.0) -> str:
    """Make sure `service_key` is up. Returns its endpoint URL."""
    endpoint = read_service(service_key)
    if endpoint and _healthy(endpoint + health_path):
        return endpoint

    cmd = _read(LOCAL_SERVICES, service_key)
    if not cmd:
        raise RuntimeError(f"no run command registered for {service_key}")

    log.info("starting dependency %s via: %s", service_key, cmd)
    # Run detached and poll registry+health endpoint until ready.
    subprocess.Popen(
        shlex.split(cmd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        endpoint = read_service(service_key)
        if endpoint and _healthy(endpoint + health_path):
            return endpoint
        time.sleep(0.5)
    raise RuntimeError(f"dependency {service_key} did not become healthy within {timeout}s")


def _healthy(url: str) -> bool:
    try:
        r = httpx.get(url, timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False
