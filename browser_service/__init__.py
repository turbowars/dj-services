"""browser_service: persistent Chrome session as a REST + MCP tool server."""
from .auth import ensure_authenticated
from .driver import DebugDriver, FrameTracker, create_driver, create_wait
from .running_env import (
    acquire_port,
    deregister_service,
    ensure_dependency,
    read_service,
    register_service,
    resolve_port,
)
from .timing import step_timer, timed

__all__ = [
    "DebugDriver",
    "FrameTracker",
    "create_driver",
    "create_wait",
    "ensure_authenticated",
    "register_service",
    "deregister_service",
    "read_service",
    "resolve_port",
    "acquire_port",
    "ensure_dependency",
    "timed",
    "step_timer",
]
