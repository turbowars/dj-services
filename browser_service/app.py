"""Standalone FastAPI app sharing the persistent Chrome session."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from selenium.common.exceptions import StaleElementReferenceException, WebDriverException

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

_ALLOWED_LOCATORS = {
    "css selector",
    "xpath",
    "id",
    "name",
    "tag name",
    "class name",
    "link text",
    "partial link text",
}


def _driver() -> DebugDriver:
    if state.driver is None:
        raise HTTPException(503, "driver not initialized")
    return state.driver


def _validated_locator(locator: str) -> str:
    if locator not in _ALLOWED_LOCATORS:
        allowed = ", ".join(sorted(_ALLOWED_LOCATORS))
        raise HTTPException(400, f"unsupported locator '{locator}', use one of: {allowed}")
    return locator


def _serialize_element(d: DebugDriver, el: Any, *, handle: str | None = None) -> dict[str, Any]:
    try:
        meta = d.raw.execute_script(
            """
            const el = arguments[0];
            return {
              tag: el.tagName ? el.tagName.toLowerCase() : null,
              id: el.id || null,
              className: el.className || null,
              name: el.getAttribute('name'),
              type: el.getAttribute('type'),
              value: el.value ?? null
            };
            """,
            el,
        )
        return {
            "handle": handle,
            "tag": meta.get("tag"),
            "id": meta.get("id"),
            "class": meta.get("className"),
            "name": meta.get("name"),
            "type": meta.get("type"),
            "value": meta.get("value"),
            "text": (el.text or "")[:200],
            "displayed": el.is_displayed(),
            "enabled": el.is_enabled(),
        }
    except StaleElementReferenceException:
        raise HTTPException(409, "element became stale")


def _resolve_target(
    d: DebugDriver,
    *,
    element_id: str | None,
    selector: str | None,
    by: str,
    index: int,
) -> tuple[Any, str | None]:
    if element_id:
        el = d.get_element(element_id)
        if el is None:
            raise HTTPException(404, f"element handle not found or stale: {element_id}")
        return el, element_id

    if not selector:
        raise HTTPException(400, "provide either element_id or selector")
    if index < 0:
        raise HTTPException(400, "index must be >= 0")

    by = _validated_locator(by)
    matches = d.raw.find_elements(by, selector)
    if not matches:
        raise HTTPException(404, f"no elements matched selector: {selector}")
    if index >= len(matches):
        raise HTTPException(400, f"index {index} out of range for {len(matches)} matches")
    el = matches[index]
    handle = d.register_element(el)
    return el, handle


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


class DomQueryRequest(BaseModel):
    selector: str
    by: str = "css selector"
    limit: int = 20
    store_handles: bool = True


@app.post("/dom/query")
def dom_query(req: DomQueryRequest) -> dict[str, Any]:
    d = _driver()
    by = _validated_locator(req.by)
    limit = max(1, min(req.limit, 200))
    matches = d.raw.find_elements(by, req.selector)

    items: list[dict[str, Any]] = []
    for el in matches[:limit]:
        handle = d.register_element(el) if req.store_handles else None
        items.append(_serialize_element(d, el, handle=handle))

    return {
        "count": len(matches),
        "returned": len(items),
        "selector": req.selector,
        "by": by,
        "elements": items,
    }


class DomTargetRequest(BaseModel):
    element_id: str | None = None
    selector: str | None = None
    by: str = "css selector"
    index: int = 0


class DomClickRequest(DomTargetRequest):
    use_js: bool = False


@app.post("/dom/click")
def dom_click(req: DomClickRequest) -> dict[str, Any]:
    d = _driver()
    el, handle = _resolve_target(
        d,
        element_id=req.element_id,
        selector=req.selector,
        by=req.by,
        index=req.index,
    )
    try:
        if req.use_js:
            d.js_click(el)
        else:
            el.click()
    except WebDriverException as exc:
        raise HTTPException(409, f"click failed: {exc}")
    d.log_step("dom click")
    return {"ok": True, "handle": handle}


class DomTypeRequest(DomTargetRequest):
    text: str
    clear_first: bool = True
    input_mode: str = "send_keys"


@app.post("/dom/type")
def dom_type(req: DomTypeRequest) -> dict[str, Any]:
    d = _driver()
    el, handle = _resolve_target(
        d,
        element_id=req.element_id,
        selector=req.selector,
        by=req.by,
        index=req.index,
    )
    if req.clear_first:
        try:
            el.clear()
        except WebDriverException:
            pass

    mode = req.input_mode.strip().lower()
    if mode not in {"send_keys", "js", "with_events"}:
        raise HTTPException(400, "input_mode must be one of: send_keys, js, with_events")

    try:
        if mode == "send_keys":
            el.send_keys(req.text)
        elif mode == "js":
            d.js_type(el, req.text)
        else:
            d.type_with_events(el, req.text)
    except WebDriverException as exc:
        raise HTTPException(409, f"type failed: {exc}")
    d.log_step("dom type")
    return {"ok": True, "handle": handle, "input_mode": mode}


class DomEventRequest(DomTargetRequest):
    event_name: str
    detail: dict[str, Any] | None = None
    bubbles: bool = True
    cancelable: bool = True


@app.post("/dom/event")
def dom_event(req: DomEventRequest) -> dict[str, Any]:
    d = _driver()
    el, handle = _resolve_target(
        d,
        element_id=req.element_id,
        selector=req.selector,
        by=req.by,
        index=req.index,
    )
    try:
        dispatched = d.raw.execute_script(
            """
            const el = arguments[0];
            const eventName = arguments[1];
            const detail = arguments[2];
            const bubbles = arguments[3];
            const cancelable = arguments[4];
            const ev = detail === null || detail === undefined
              ? new Event(eventName, { bubbles, cancelable })
              : new CustomEvent(eventName, { bubbles, cancelable, detail });
            return el.dispatchEvent(ev);
            """,
            el,
            req.event_name,
            req.detail,
            req.bubbles,
            req.cancelable,
        )
    except WebDriverException as exc:
        raise HTTPException(409, f"event dispatch failed: {exc}")
    d.log_step(f"dom event {req.event_name}")
    return {"ok": True, "handle": handle, "dispatched": bool(dispatched)}


@app.get("/dom/element/{element_id}")
def dom_element(element_id: str) -> dict[str, Any]:
    d = _driver()
    el = d.get_element(element_id)
    if el is None:
        raise HTTPException(404, f"element handle not found or stale: {element_id}")
    return _serialize_element(d, el, handle=element_id)


@app.delete("/dom/element/{element_id}")
def dom_release_element(element_id: str) -> dict[str, Any]:
    d = _driver()
    d.release_element(element_id)
    return {"ok": True, "released": element_id}


@app.post("/dom/clear-handles")
def dom_clear_handles() -> dict[str, Any]:
    d = _driver()
    d.clear_elements()
    return {"ok": True}


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
