"""Per-client rate limits for the routes worth guessing at or flooding, for deployments without a
WAF in front (one VM, a bare Helm install). A WAF's limits still apply on top where there is one.

Two groups, each a token bucket that refills evenly over a minute:

- ``auth``: sign-in and the OAuth endpoints, keyed by client IP
  (``SATCHEL_RATE_LIMIT_AUTH_PER_MIN``, default 30).
- ``mcp``: ``/mcp``, keyed by the bearer token when there is one (so a team behind one NAT is
  not one client), else by IP (``SATCHEL_RATE_LIMIT_MCP_PER_MIN``, default 600).

0 turns a group off. Limits are per server process: N replicas allow up to N times the rate.
The client IP is uvicorn's, so behind a proxy set ``SATCHEL_FORWARDED_ALLOW_IPS`` or every
request looks like the proxy's.

Pure ASGI, so ``/mcp``'s streamed responses pass through untouched.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]

AUTH_PATHS = ("/auth/login/start", "/auth/callback", "/oauth/")
MAX_KEYS = 50_000  # past this, idle buckets are dropped


def group_of(path: str) -> str | None:
    if path == "/mcp" or path.startswith("/mcp/"):
        return "mcp"
    if path.startswith(AUTH_PATHS):
        return "auth"
    return None


class TokenBuckets:
    """``per_min`` requests per minute per key, with bursts up to ``per_min``."""

    def __init__(self, per_min: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.capacity = float(per_min)
        self.rate = per_min / 60.0
        self.clock = clock
        self._b: dict[str, tuple[float, float]] = {}

    def take(self, key: str) -> float:
        """0 when allowed, else the seconds until a request would be."""
        now = self.clock()
        tokens, last = self._b.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens >= 1:
            self._b[key] = (tokens - 1, now)
            if len(self._b) > MAX_KEYS:
                self._prune(now)
            return 0.0
        self._b[key] = (tokens, now)
        return (1 - tokens) / self.rate

    def _prune(self, now: float) -> None:
        full = self.capacity / self.rate  # seconds for an empty bucket to refill
        self._b = {k: v for k, v in self._b.items() if now - v[1] < full}


class RateLimitMiddleware:
    def __init__(self, app: App, *, auth_per_min: int, mcp_per_min: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.app = app
        self.limits = {name: TokenBuckets(n, clock) for name, n in
                       (("auth", auth_per_min), ("mcp", mcp_per_min)) if n > 0}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        group = group_of(scope.get("path", "")) if scope["type"] == "http" else None
        buckets = self.limits.get(group) if group else None
        if buckets is None or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return
        wait = buckets.take(f"{group}:{_client_key(scope, by_token=group == 'mcp')}")
        if wait <= 0:
            await self.app(scope, receive, send)
            return
        retry = max(1, math.ceil(wait))
        body = json.dumps({"detail": f"Too many requests: try again in {retry} s."}).encode()
        await send({"type": "http.response.start", "status": 429, "headers": [
            (b"content-type", b"application/json"), (b"retry-after", str(retry).encode()),
            (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def _client_key(scope: Scope, *, by_token: bool) -> str:
    if by_token:
        for k, v in scope.get("headers", []):
            if k.lower() == b"authorization" and v:
                return "t:" + hashlib.sha256(v).hexdigest()[:32]
    client = scope.get("client")
    return "ip:" + (client[0] if client else "unknown")
