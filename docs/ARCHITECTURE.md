# Architecture

## High-level view

```mermaid
flowchart LR
    C[Client Service\nPython/Node/Curl] -->|HTTP JSON| A[FastAPI App\nbrowser_service/app.py]
    A --> D[DebugDriver\nbrowser_service/driver.py]
    D --> S[Selenium WebDriver]
    S --> CH[Chrome\nPersistent Profile]

    A --> R[Service Registry\nbrowser_service/running_env.py]
    R --> RE[~/.running_env]
    R --> LS[~/.local-services]

    A --> AU[Auth Helper\nbrowser_service/auth.py]
    AU --> CH

    D --> DBG[debug/\nPNG + HTML snapshots]
```

## DOM handle architecture

```mermaid
sequenceDiagram
    participant Client
    participant API as FastAPI /dom/*
    participant DD as DebugDriver
    participant DOM as Browser DOM

    Client->>API: POST /dom/query {selector, by}
    API->>DOM: find_elements(by, selector)
    API->>DD: register_element(el)
    DD-->>API: handle (uuid)
    API-->>Client: elements[] with handle

    Client->>API: POST /dom/click or /dom/type or /dom/event
    API->>DD: get_element(handle)
    DD->>DOM: staleness check
    alt valid handle
        API->>DOM: perform action/event
        API-->>Client: {ok:true}
    else stale or missing
        API-->>Client: 404/409
    end

    Client->>API: POST /navigate
    API->>DD: driver.get(url)
    DD->>DD: clear_elements()
    API-->>Client: new page state
```

## Runtime lifecycle

1. Startup
   - App creates one long-lived DebugDriver instance.
   - In connect mode, app attaches to a detached Chrome through DevTools port.
   - Optional auth warmup runs when BROWSER_TARGET_URL is provided.

2. Service registration
   - Endpoint is registered in ~/.running_env with key browser_service.
   - Restart command is written to ~/.local-services.

3. Request handling
   - FastAPI endpoints delegate browser actions to DebugDriver.
   - DOM endpoints optionally issue and reuse in-memory element handles.

4. Shutdown
   - Service deregisters from ~/.running_env.
   - In connect mode, Chrome stays alive.
   - In launch mode, browser is closed with the service.

## Module responsibilities

- browser_service/app.py
  - API schema and endpoint contracts
  - input validation
  - translating Selenium exceptions into HTTP errors

- browser_service/driver.py
  - Chrome startup/attachment
  - frame tracking
  - debug snapshots
  - element handle registry and stale checks

- browser_service/auth.py
  - startup authentication heuristic
  - optional login callback support
  - interactive MFA wait loop

- browser_service/running_env.py
  - endpoint registration/discovery
  - port conflict resolution
  - dependency health checking

- browser_service/timing.py
  - lightweight timing decorators and context manager

## Design decisions

- Persistent browser profile to preserve auth/session state.
- In-memory element handles for convenience and speed.
- Handle invalidation on navigation to prevent unsafe stale reuse.
- Registry files for local service discovery across cooperating tools.
