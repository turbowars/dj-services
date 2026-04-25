# API Reference

Base URL examples:

- http://127.0.0.1:8079
- value from ~/.running_env entry: browser_service=http://host:port

Content type for POST requests:

- application/json

## Conventions

- All endpoints return JSON.
- DOM targeting supports two modes:
  1. by element_id handle (preferred for repeated actions)
  2. by selector + by + index
- Allowed locator values for by:
  - css selector
  - xpath
  - id
  - name
  - tag name
  - class name
  - link text
  - partial link text

## Service and page metadata

### GET /health

Returns service and browser health.

Response fields:

- ok: boolean
- url: current page URL when healthy
- title: page title when healthy
- mode: connect or launch
- reason: error string when not healthy

### GET /page/state

Returns current page URL and title.

Response:

```json
{ "url": "https://example.com", "title": "Example" }
```

### GET /cookies

Returns browser cookies and best-effort CSRF token from meta tags.

Response fields:

- cookies: Selenium cookie objects
- csrf_token: string or null

## Navigation and snapshots

### POST /navigate

Navigate to a URL.

Request:

```json
{ "url": "https://example.com" }
```

Response:

```json
{ "ok": true, "url": "https://example.com", "title": "Example" }
```

Behavior note:

- A top-level navigation clears all DOM element handles.

### POST /screenshot

Capture PNG + HTML snapshot in debug directory.

Request:

```json
{ "label": "after-login" }
```

Response:

```json
{
  "png": "/abs/path/debug/123456_after-login.png",
  "html": "/abs/path/debug/123456_after-login.html"
}
```

## DOM API

### POST /dom/query

Find elements and optionally store reusable handles.

Request:

```json
{
  "selector": "button.submit",
  "by": "css selector",
  "limit": 20,
  "store_handles": true
}
```

Response:

```json
{
  "count": 3,
  "returned": 3,
  "selector": "button.submit",
  "by": "css selector",
  "elements": [
    {
      "handle": "4a57...",
      "tag": "button",
      "id": "submit",
      "class": "btn primary",
      "name": null,
      "type": "submit",
      "value": null,
      "text": "Submit",
      "displayed": true,
      "enabled": true
    }
  ]
}
```

Usage note:

- If store_handles=false, handle is returned as null.

### POST /dom/click

Click an element.

Request by handle:

```json
{ "element_id": "4a57...", "use_js": false }
```

Request by selector:

```json
{
  "selector": "button.submit",
  "by": "css selector",
  "index": 0,
  "use_js": true
}
```

Behavior:

- use_js=false: Selenium click()
- use_js=true: JavaScript arguments[0].click()

Response:

```json
{ "ok": true, "handle": "4a57..." }
```

### POST /dom/type

Type text into an element.

Request:

```json
{
  "element_id": "4a57...",
  "text": "hello world",
  "clear_first": true,
  "input_mode": "with_events"
}
```

input_mode values:

- send_keys: Selenium send_keys
- js: set value via JavaScript and dispatch input/change
- with_events: keydown/input/keyup simulation for each char

Response:

```json
{ "ok": true, "handle": "4a57...", "input_mode": "with_events" }
```

### POST /dom/event

Dispatch DOM events on an element.

Request:

```json
{
  "element_id": "4a57...",
  "event_name": "mouseenter",
  "detail": { "source": "service-a" },
  "bubbles": true,
  "cancelable": true
}
```

Behavior:

- If detail is null/omitted: Event
- If detail is provided: CustomEvent with detail payload

Response:

```json
{ "ok": true, "handle": "4a57...", "dispatched": true }
```

### GET /dom/element/{element_id}

Return current metadata/state for a stored handle.

Response shape is same as an item in /dom/query elements.

### DELETE /dom/element/{element_id}

Release one handle.

Response:

```json
{ "ok": true, "released": "4a57..." }
```

### POST /dom/clear-handles

Release all handles.

Response:

```json
{ "ok": true }
```

## DOM access workflow for client teams

Recommended sequence:

1. Query once with /dom/query and keep returned handle.
2. Reuse handle for /dom/click, /dom/type, /dom/event.
3. If you get 404 or 409, re-run /dom/query and retry.
4. Clear handles explicitly when done if your workflow stores many.

Why this works:

- selector lookup cost is paid once
- avoids repeated locator drift where possible
- keeps call payloads small and explicit

## Error model

Common status codes:

- 400: invalid request, unsupported locator, bad index, invalid mode
- 404: element handle not found or selector matched nothing
- 409: stale element or Selenium action failure
- 503: driver not initialized

Typical error payload:

```json
{ "detail": "element handle not found or stale: 4a57..." }
```

## Curl examples

### Find, click, type, and dispatch an event

```bash
BASE="http://127.0.0.1:8079"

Q=$(curl -sS -X POST "$BASE/dom/query" \
  -H 'Content-Type: application/json' \
  -d '{"selector":"input[name=email]","by":"css selector","store_handles":true}')

HANDLE=$(python - <<'PY'
import json,sys
print(json.loads(sys.stdin.read())["elements"][0]["handle"])
PY
<<< "$Q")

curl -sS -X POST "$BASE/dom/type" \
  -H 'Content-Type: application/json' \
  -d "{\"element_id\":\"$HANDLE\",\"text\":\"user@example.com\",\"input_mode\":\"with_events\"}"

curl -sS -X POST "$BASE/dom/event" \
  -H 'Content-Type: application/json' \
  -d "{\"element_id\":\"$HANDLE\",\"event_name\":\"blur\",\"bubbles\":true}"
```

## Operational notes

- DOM handles are process-local in-memory references.
- Restarting the service invalidates all existing handles.
- Top-level navigation clears handles automatically.
- Use selector fallback logic in clients for resilience.
