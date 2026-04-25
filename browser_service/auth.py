"""Pluggable authentication: URL-based detection, interactive MFA fallback."""
from __future__ import annotations

import getpass
import logging
import os
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .driver import DebugDriver

log = logging.getLogger("browser_service.auth")

CREDS_FILE = Path.home() / ".user"
DEFAULT_MFA_TIMEOUT = float(os.environ.get("BROWSER_AUTH_MFA_TIMEOUT", "300"))


def _read_credentials(path: Path = CREDS_FILE) -> dict[str, str]:
    # Simple KEY=VALUE parser to stay shell-friendly and dependency-free.
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _write_credentials(data: dict[str, str], path: Path = CREDS_FILE) -> None:
    # Restrict file permissions because secrets are stored in plain text.
    path.write_text("\n".join(f"{k}={v}" for k, v in data.items()) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def get_or_prompt_credentials(
    username_key: str = "username",
    password_key: str = "password",
) -> tuple[str, str]:
    data = _read_credentials()
    if username_key in data and password_key in data:
        return data[username_key], data[password_key]
    print(f"Credentials for {username_key} not found in {CREDS_FILE}; prompting once.")
    username = input(f"{username_key}: ").strip()
    password = getpass.getpass(f"{password_key}: ")
    data.update({username_key: username, password_key: password})
    _write_credentials(data)
    return username, password


def _host_of(url: str) -> str:
    return urlparse(url).hostname or ""


def _looks_authenticated(driver: DebugDriver, target_url: str) -> bool:
    # Heuristic: same host and not on obvious auth/login URLs.
    current = driver.raw.current_url or ""
    current_host = _host_of(current)
    target_host = _host_of(target_url)
    if not current_host or not target_host:
        return False
    if current_host != target_host:
        return False
    low = current.lower()
    return not any(marker in low for marker in ("/login", "/signin", "/sso", "/auth"))


def ensure_authenticated(
    driver: DebugDriver,
    target_url: str,
    *,
    login_flow: Callable[[DebugDriver], None] | None = None,
    mfa_timeout: float = DEFAULT_MFA_TIMEOUT,
    poll_interval: float = 2.0,
) -> bool:
    """Navigate to target_url and make sure the session is authenticated.

    `login_flow` is an optional callback that performs app-specific login
    (fill username/password, click submit). After it returns, the caller
    may still need to complete MFA interactively.
    """
    driver.log_step(f"auth: navigate to {target_url}")
    driver.get(target_url)
    driver.capture_debug_snapshot("auth_initial")

    if _looks_authenticated(driver, target_url):
        log.info("auth: already authenticated via persistent profile")
        return True

    if login_flow is not None:
        try:
            # Optional site-specific automation hook.
            login_flow(driver)
        except Exception as exc:
            log.warning("auth: login_flow raised: %s", exc)
        driver.capture_debug_snapshot("auth_after_flow")

    print(
        "\n>>> Complete any remaining login / MFA steps in the browser window. "
        f"Waiting up to {int(mfa_timeout)}s...\n"
    )
    deadline = time.time() + mfa_timeout
    while time.time() < deadline:
        # Poll while the user completes CAPTCHA/MFA in the real browser.
        if _looks_authenticated(driver, target_url):
            driver.capture_debug_snapshot("auth_success")
            log.info("auth: session authenticated")
            return True
        time.sleep(poll_interval)

    driver.capture_debug_snapshot("auth_timeout")
    log.error("auth: timed out waiting for authenticated session")
    return False
