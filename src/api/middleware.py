"""aiohttp middleware: API-wide client concurrency cap and per-route
request timeouts. Kept out of `main.py`/`app.py` per AGENTS.md.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Final

from aiohttp import web
from aiohttp.typedefs import Handler, Middleware

logger = logging.getLogger(__name__)

RouteTimeoutAttr = "smores_timeout_seconds"
"""Attribute name `with_timeout` sets on a decorated handler function;
read back by the timeout middleware to find a per-route override."""

_RETRY_AFTER_SECONDS: Final[int] = 1

type RouteTimeout = float | Callable[[web.Request], float]
"""A fixed timeout in seconds, or a function computing one per request
(e.g. `/api/scan`, whose worst case depends on its address-range query
params)."""


def with_timeout(seconds: RouteTimeout) -> Callable[[Handler], Handler]:
    """Decorator marking a route handler's timeout override (e.g.
    `/api/sensors/current`'s `config.poll_timeout_seconds`), read by
    `create_timeout_middleware`. Undecorated handlers use that
    middleware's `default_timeout_seconds`. `seconds` may also be a
    callable taking the `web.Request`, evaluated before the handler runs.

    Since it only tags the handler object and returns it unchanged, it also
    works as a plain function call at route-registration time
    (`with_timeout(config.poll_timeout_seconds)(handler)`), which is how
    `api.routes.register_routes` applies it — the override seconds come
    from the `Config` loaded at process startup, not a literal available
    when this module is imported.
    """

    def decorator(handler: Handler) -> Handler:
        setattr(handler, RouteTimeoutAttr, seconds)
        return handler

    return decorator


def create_timeout_middleware(default_timeout_seconds: float) -> Middleware:
    """Build a `@web.middleware` that runs each handler under
    `asyncio.wait_for`, using the handler's `with_timeout`-set override if
    present, else `default_timeout_seconds`.

    On expiry, responds `504 Gateway Timeout` with a JSON error body
    instead of propagating `TimeoutError`.
    """

    @web.middleware
    async def timeout_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        route_handler = request.match_info.handler
        timeout: RouteTimeout = getattr(route_handler, RouteTimeoutAttr, default_timeout_seconds)
        timeout_seconds = timeout(request) if callable(timeout) else timeout
        try:
            return await asyncio.wait_for(handler(request), timeout=timeout_seconds)
        except TimeoutError:
            logger.warning(
                "Request to %s %s timed out after %gs",
                request.method,
                request.path,
                timeout_seconds,
            )
            return web.json_response(
                {
                    "error": "Gateway Timeout",
                    "detail": f"Request exceeded its {timeout_seconds:g}s timeout",
                },
                status=504,
            )

    return timeout_middleware


def create_concurrency_limit_middleware(max_concurrent_clients: int) -> Middleware:
    """Build a `@web.middleware` enforcing at most `max_concurrent_clients`
    requests in flight at once via an `asyncio.Semaphore`.

    A request arriving with no capacity available gets an immediate
    `503 Service Unavailable` with a `Retry-After` header — never queued —
    per AGENTS.md.
    """
    semaphore = asyncio.Semaphore(max_concurrent_clients)

    @web.middleware
    async def concurrency_limit_middleware(
        request: web.Request, handler: Handler
    ) -> web.StreamResponse:
        # `asyncio.wait_for(semaphore.acquire(), timeout=0)` looks like the
        # obvious non-blocking try-acquire but is actually always a timeout:
        # wait_for wraps the coroutine in a fresh Task, and a Task can never
        # be `done()` before it gets its first event-loop iteration, so the
        # `timeout <= 0` fast path in wait_for cancels it before it runs even
        # when the semaphore has free capacity. Peeking at `_value` instead
        # is safe here because nothing between the check and `acquire()`
        # awaits, so no other task can run in between on this single-threaded
        # event loop.
        if semaphore._value <= 0:
            logger.warning(
                "Rejecting %s %s: at capacity (%d concurrent clients)",
                request.method,
                request.path,
                max_concurrent_clients,
            )
            return web.json_response(
                {
                    "error": "Service Unavailable",
                    "detail": "Server is at maximum concurrent client capacity",
                },
                status=503,
                headers={"Retry-After": str(_RETRY_AFTER_SECONDS)},
            )
        await semaphore.acquire()
        try:
            return await handler(request)
        finally:
            semaphore.release()

    return concurrency_limit_middleware


__all__ = [
    "create_concurrency_limit_middleware",
    "create_timeout_middleware",
    "with_timeout",
]
