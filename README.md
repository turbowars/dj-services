# browser_service

Persistent Chrome session exposed as a FastAPI service so other local tools/services can:

- navigate pages
- inspect page state and cookies
- capture debug snapshots
- query DOM elements and store reusable element handles
- trigger interactions like click, type, and custom DOM events

This project is designed for local automation against authenticated browser sessions where a persistent Chrome profile is valuable.

## Project layout

- browser_service/app.py: FastAPI app and REST endpoints
- browser_service/driver.py: Chrome lifecycle, Selenium wrapper, frame tracking, element handle registry
- browser_service/auth.py: optional auth warmup and interactive MFA waiting
- browser_service/running_env.py: service registry and port management using ~/.running_env and ~/.local-services
- browser_service/timing.py: timing helpers
- debug/: captured screenshot/html snapshots
- docs/ARCHITECTURE.md: architecture and flow diagram
- docs/API.md: detailed API reference and usage examples

## Requirements

- Python 3.11+
- Google Chrome installed (or CHROME_BINARY configured)
- Matching ChromeDriver available in PATH

Dependencies are listed in requirements.txt and pyproject.toml.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run the service

```bash
.venv/bin/python -m uvicorn browser_service.app:app --host 0.0.0.0 --port 8079
```

When the service starts, it:

1. creates/attaches to a persistent Chrome session
2. optionally warms up authentication if BROWSER_TARGET_URL is set
3. registers its endpoint in ~/.running_env as browser_service=http://host:port
4. stores a restart command in ~/.local-services

## Core behavior

- Driver lifecycle:
  - connect mode (default): attaches Selenium to detached Chrome via DevTools port and keeps Chrome alive on shutdown
  - launch mode: Selenium manages the browser lifecycle
- Port behavior:
  - uses BROWSER_SERVICE_PORT if set
  - otherwise attempts to reuse previously registered port
  - falls back to next available port if needed
- Debug snapshots:
  - screenshot endpoint writes both PNG and HTML under debug/
- DOM handles:
  - querying endpoints can return a reusable element handle
  - handles are in-memory only and tied to current process/page lifecycle
  - handles are cleared automatically on top-level navigation

## Key environment variables

- CHROME_DEBUG_PORT: DevTools port for connect mode (default 9222)
- CHROME_BINARY: explicit Chrome binary path
- BROWSER_MODE: connect or launch (default connect)
- BROWSER_SERVICE_HOST: FastAPI bind host (default 0.0.0.0)
- BROWSER_SERVICE_PORT: FastAPI bind port (default 8079 unless reused/fallback)
- BROWSER_TARGET_URL: optional URL to navigate/authenticate during startup
- BROWSER_AUTH_MFA_TIMEOUT: wait time for interactive MFA in seconds (default 300)
- BROWSER_DEBUG_DIR: snapshot output directory (default ./debug)
- BROWSER_LOG_LEVEL: logging level

## API overview

Health and metadata:

- GET /health
- GET /page/state
- GET /cookies

Navigation and debug:

- POST /navigate
- POST /screenshot

DOM interaction:

- POST /dom/query
- POST /dom/click
- POST /dom/type
- POST /dom/event
- GET /dom/element/{element_id}
- DELETE /dom/element/{element_id}
- POST /dom/clear-handles

For full request/response documentation and examples, see docs/API.md.

For architecture and flow diagrams, see docs/ARCHITECTURE.md.

## Quick DOM usage example

```bash
# 1) Find and store element handle
curl -sS -X POST http://127.0.0.1:8079/dom/query \
  -H 'Content-Type: application/json' \
  -d '{"selector":"button.submit","by":"css selector","store_handles":true}'

# 2) Click by element handle
curl -sS -X POST http://127.0.0.1:8079/dom/click \
  -H 'Content-Type: application/json' \
  -d '{"element_id":"<handle-from-query>"}'

# 3) Dispatch a custom event
curl -sS -X POST http://127.0.0.1:8079/dom/event \
  -H 'Content-Type: application/json' \
  -d '{"element_id":"<handle>","event_name":"my-event","detail":{"source":"api"}}'
```

## Notes and constraints

- This service is intended for trusted local environments.
- There is no built-in auth on the API endpoints.
- Element handles are ephemeral and not portable across process restarts.
- A page navigation invalidates prior handles and can cause stale-element behavior.

## Recommended next step

If external teams will consume this API broadly, add an OpenAPI client generation step and authentication/rate limits in front of this service.
