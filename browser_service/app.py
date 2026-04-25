"""Standalone FastAPI app sharing the persistent Chrome session."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from selenium.common.exceptions import WebDriverException

from .auth import ensure_authenticated
from .driver import DEFAULT_DEBUG_PORT, DEFAULT_PROFILE_DIR, DebugDriver, create_driver
from .running_env import acquire_port, deregister_service, register_service, resolve_port

log = logging.getLogger("browser_service.app")
logging.basicConfig(level=os.environ.get("BROWSER_LOG_LEVEL", "INFO"))

SERVICE_KEY = "browser_service"
DEFAULT_PORT = 8079
DEFAULT_HOST = os.environ.get("BROWSER_SERVICE_HOST", "0.0.0.0")
TARGET_URL = os.environ.get("BROWSER_TARGET_URL", "about:blank")


class _State:
    driver: DebugDriver | None = None


state = _State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create one long-lived browser driver for the entire API process.
    state.driver = create_driver(port=DEFAULT_DEBUG_PORT, profile_dir=DEFAULT_PROFILE_DIR)
    # Optionally warm up/authenticate the browser session on startup.
    if TARGET_URL and TARGET_URL != "about:blank":
        ensure_authenticated(state.driver, TARGET_URL)

    # Prefer configured/previous port, then reclaim/fallback if needed.
    preferred = resolve_port("BROWSER_SERVICE_PORT", SERVICE_KEY, DEFAULT_PORT)
    port = acquire_port(preferred, ["browser_service.app", f"port={preferred}"], SERVICE_KEY)
    run_cmd = f"python -m uvicorn browser_service.app:app --host {DEFAULT_HOST} --port {port}"
    # Publish endpoint so sibling local services can discover/restart it.
    register_service(SERVICE_KEY, DEFAULT_HOST, port, run_cmd=run_cmd)
    app.state.bound_port = port

    try:
        yield
    finally:
        deregister_service(SERVICE_KEY)
        if state.driver is not None:
            state.driver.quit()
            state.driver = None


app = FastAPI(title="browser_service", lifespan=lifespan)


def _driver() -> DebugDriver:
    if state.driver is None:
        raise HTTPException(503, "driver not initialized")
    return state.driver


@app.get("/health")
def health() -> dict[str, Any]:
    d = state.driver
    if d is None:
        return {"ok": False, "reason": "driver not initialized"}
    try:
        # Keep health lightweight: current page metadata proves browser control.
        return {"ok": True, "url": d.raw.current_url, "title": d.raw.title, "mode": d.mode}
    except WebDriverException as exc:
        return {"ok": False, "reason": str(exc)}


@app.get("/cookies")
def cookies() -> dict[str, Any]:
    d = _driver()
    cookies = d.raw.get_cookies()
    csrf_token: str | None = None
    try:
        # Common helper for apps that store CSRF token in a meta tag.
        csrf_token = d.raw.execute_script(
            "const m = document.querySelector('meta[name=csrf-token], meta[name=csrf_token]');"
            "return m ? m.getAttribute('content') : null;"
        )
    except WebDriverException:
        pass
    return {"cookies": cookies, "csrf_token": csrf_token}


@app.get("/page/state")
def page_state() -> dict[str, str]:
    d = _driver()
    return {"url": d.raw.current_url, "title": d.raw.title}


class NavigateRequest(BaseModel):
    url: str


@app.post("/navigate")
def navigate(req: NavigateRequest) -> dict[str, Any]:
    d = _driver()
    d.get(req.url)
    # Structured step log improves traceability when reviewing debug snapshots.
    d.log_step(f"navigate {req.url}")
    return {"ok": True, "url": d.raw.current_url, "title": d.raw.title}


class ScreenshotRequest(BaseModel):
    label: str = "snapshot"


@app.post("/screenshot")
def screenshot(req: ScreenshotRequest) -> dict[str, str]:
    d = _driver()
    base = d.capture_debug_snapshot(req.label)
    return {"png": str(base.with_suffix(".png")), "html": str(base.with_suffix(".html"))}
