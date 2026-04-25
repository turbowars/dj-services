"""Integration tests for browser_service — requires the service to be running on port 8079."""
import httpx
import pytest

BASE = "http://localhost:8079"


@pytest.fixture(scope="session")
def client():
    # connect=10s, read=60s — screenshot + real page loads can be slow
    timeout = httpx.Timeout(60.0, connect=10.0)
    with httpx.Client(base_url=BASE, timeout=timeout) as c:
        yield c


@pytest.fixture(scope="session")
def on_example(client):
    """Navigate to example.com once; reused by all tests that need a stable page."""
    client.post("/navigate", json={"url": "https://www.example.com"})


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True, f"Service unhealthy: {body.get('reason')}"
    assert "url" in body
    assert "title" in body
    assert body["mode"] in ("connect", "launch")


def test_navigate_google(client):
    r = client.post("/navigate", json={"url": "https://www.google.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "google.com" in body["url"]
    assert body["title"] != ""


def test_navigate_espn(client):
    r = client.post("/navigate", json={"url": "https://www.espn.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "espn.com" in body["url"]


def test_page_state_reflects_navigation(client, on_example):
    r = client.get("/page/state")
    assert r.status_code == 200
    body = r.json()
    assert "example.com" in body["url"]
    assert body["title"] != ""


def test_dom_query_finds_elements(client, on_example):
    r = client.post("/dom/query", json={"selector": "h1", "by": "css selector"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    assert len(body["elements"]) >= 1
    assert body["elements"][0]["tag"] == "h1"


def test_dom_query_returns_handle(client, on_example):
    r = client.post("/dom/query", json={"selector": "h1", "by": "css selector", "store_handles": True})
    assert r.status_code == 200
    handle = r.json()["elements"][0]["handle"]
    assert handle is not None

    r2 = client.get(f"/dom/element/{handle}")
    assert r2.status_code == 200
    assert r2.json()["tag"] == "h1"


def test_dom_query_invalid_locator(client):
    r = client.post("/dom/query", json={"selector": "h1", "by": "invalid"})
    assert r.status_code == 400


def test_screenshot_returns_paths(client, on_example):
    r = client.post("/screenshot", json={"label": "test-snap"})
    assert r.status_code == 200
    body = r.json()
    assert "png_ok" in body
    assert "html" in body
    assert body["html"].endswith(".html")
    if not body["png_ok"]:
        assert body["png"] is None


def test_cookies(client, on_example):
    r = client.get("/cookies")
    assert r.status_code == 200
    body = r.json()
    assert "cookies" in body
    assert isinstance(body["cookies"], list)


def test_navigate_bad_url_does_not_crash(client):
    # chrome://version/ loads instantly without network, confirms driver stays alive
    client.post("/navigate", json={"url": "chrome://version/"})
    r2 = client.get("/health")
    assert r2.status_code == 200

