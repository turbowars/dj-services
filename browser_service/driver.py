"""Chrome lifecycle, DebugDriver wrapper, and FrameTracker."""
from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import (
    JavascriptException,
    NoSuchFrameException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

log = logging.getLogger("browser_service.driver")

DEFAULT_DEBUG_PORT = int(os.environ.get("CHROME_DEBUG_PORT", "9222"))
DEFAULT_PROFILE_DIR = Path.home() / ".browser_service_profile"
DEBUG_DIR = Path(os.environ.get("BROWSER_DEBUG_DIR", "./debug")).resolve()


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _clear_stale_locks(profile_dir: Path) -> None:
    # Chrome can leave lock files behind after crashes; clear best-effort.
    for name in ("LOCK", "SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = profile_dir / name
        if p.exists() or p.is_symlink():
            try:
                p.unlink()
                log.info("cleared stale lock %s", p.name)
            except OSError:
                pass


def _chrome_binary() -> str:
    # Priority: explicit env var, then common platform install locations.
    env = os.environ.get("CHROME_BINARY")
    if env:
        return env
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        shutil.which("google-chrome") or "",
        shutil.which("chromium") or "",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise RuntimeError("Chrome binary not found; set CHROME_BINARY")


def _launch_detached_chrome(port: int, profile_dir: Path) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    _clear_stale_locks(profile_dir)
    args = [
        _chrome_binary(),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-features=InfiniteSessionRestore",
    ]
    log.info("launching Chrome: port=%d profile=%s", port, profile_dir)
    subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 20
    # Wait until the DevTools port is reachable before Selenium attach.
    while time.time() < deadline:
        if _port_open("127.0.0.1", port):
            return
        time.sleep(0.25)
    raise RuntimeError(f"Chrome did not open debug port {port} within 20s")


def _connect(port: int) -> webdriver.Chrome:
    # Attach Selenium to an already-running Chrome via DevTools.
    opts = Options()
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    return webdriver.Chrome(options=opts)


def _launch_managed(profile_dir: Path) -> webdriver.Chrome:
    # Launch Chrome fully managed by Selenium (driver owns browser lifecycle).
    profile_dir.mkdir(parents=True, exist_ok=True)
    _clear_stale_locks(profile_dir)
    opts = Options()
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--profile-directory=Default")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-session-crashed-bubble")
    opts.add_argument("--disable-features=InfiniteSessionRestore")
    return webdriver.Chrome(options=opts)


def create_driver(
    port: int = DEFAULT_DEBUG_PORT,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    mode: str | None = None,
) -> "DebugDriver":
    # connect: reuse a detached Chrome + persistent profile.
    # launch: let Selenium spawn and own Chrome directly.
    mode = (mode or os.environ.get("BROWSER_MODE", "connect")).lower()
    if mode == "connect":
        if not _port_open("127.0.0.1", port):
            _launch_detached_chrome(port, profile_dir)
        raw = _connect(port)
    elif mode == "launch":
        raw = _launch_managed(profile_dir)
    else:
        raise ValueError(f"unknown BROWSER_MODE: {mode!r}")
    return DebugDriver(raw, mode=mode)


def create_wait(driver: "DebugDriver", timeout: float = 15.0) -> WebDriverWait:
    return WebDriverWait(driver.raw, timeout)


class FrameTracker:
    """Tracks which iframe the driver is currently focused on."""

    def __init__(self, driver: "DebugDriver") -> None:
        self._driver = driver
        self._state: str = "unknown"

    @property
    def current(self) -> str:
        return self._state

    def invalidate(self) -> None:
        self._state = "unknown"

    def _alive(self) -> bool:
        try:
            self._driver.raw.execute_script("return 1")
            return True
        except (JavascriptException, WebDriverException):
            return False

    def ensure_default(self) -> None:
        # Avoid redundant frame switching when we already know state is valid.
        if self._state == "default" and self._alive():
            return
        self._driver.raw.switch_to.default_content()
        self._state = "default"

    def ensure_frame(self, name: str, timeout: float = 10.0) -> bool:
        if self._state == name and self._alive():
            return True
        self.ensure_default()
        deadline = time.time() + timeout
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                self._driver.raw.switch_to.frame(name)
                self._state = name
                return True
            except (NoSuchFrameException, WebDriverException) as exc:
                last_err = exc
                time.sleep(0.25)
        log.warning("ensure_frame(%s) failed: %s", name, last_err)
        return False


class DebugDriver:
    """WebDriver wrapper that logs steps and captures debug snapshots."""

    def __init__(self, raw: webdriver.Chrome, mode: str = "connect") -> None:
        self.raw = raw
        self.mode = mode
        self._step = 0
        self.frames = FrameTracker(self)
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    def __getattr__(self, item: str) -> Any:
        return getattr(self.raw, item)

    def get(self, url: str) -> None:
        self.raw.get(url)
        self.frames.invalidate()

    def log_step(self, desc: str) -> None:
        self._step += 1
        try:
            log.info("[%02d] %s | %s | %s", self._step, desc, self.raw.current_url, self.raw.title)
        except WebDriverException:
            log.info("[%02d] %s", self._step, desc)

    def capture_debug_snapshot(self, label: str) -> Path:
        # Pair screenshot + DOM snapshot to speed up post-failure diagnosis.
        stamp = datetime.now().strftime("%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:80]
        base = DEBUG_DIR / f"{stamp}_{safe}"
        try:
            self.raw.save_screenshot(str(base.with_suffix(".png")))
        except WebDriverException as exc:
            log.warning("screenshot failed: %s", exc)
        try:
            base.with_suffix(".html").write_text(self.raw.page_source, encoding="utf-8")
        except WebDriverException as exc:
            log.warning("page_source failed: %s", exc)
        return base

    def log_elements(self, css: str) -> None:
        els = self.raw.find_elements("css selector", css)
        log.info("selector %r matched %d", css, len(els))
        for i, el in enumerate(els):
            try:
                log.info(
                    "  [%d] <%s id=%r class=%r> %r",
                    i,
                    el.tag_name,
                    el.get_attribute("id"),
                    el.get_attribute("class"),
                    (el.text or "")[:80],
                )
            except WebDriverException:
                continue

    def js_click(self, el: WebElement) -> None:
        self.raw.execute_script("arguments[0].click();", el)

    def js_type(self, el: WebElement, text: str) -> None:
        self.raw.execute_script(
            """
            const el = arguments[0];
            el.value = arguments[1];
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            """,
            el,
            text,
        )

    def type_with_events(self, el: WebElement, text: str, delay: float = 0.02) -> None:
        el.click()
        for ch in text:
            self.raw.execute_script(
                """
                const el = arguments[0];
                const ch = arguments[1];
                el.dispatchEvent(new KeyboardEvent('keydown', {key: ch, bubbles: true}));
                el.value = (el.value || '') + ch;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new KeyboardEvent('keyup', {key: ch, bubbles: true}));
                """,
                el,
                ch,
            )
            if delay:
                time.sleep(delay)

    def quit(self) -> None:
        # In connect mode we intentionally keep the user's Chrome session alive.
        if self.mode == "connect":
            log.info("connect mode: leaving Chrome alive")
            return
        try:
            self.raw.quit()
        except WebDriverException as exc:
            log.warning("quit failed: %s", exc)
