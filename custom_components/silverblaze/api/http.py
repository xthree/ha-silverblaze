"""The single request path: policy-checked, paced, retried, redacted."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import aiohttp

from .allowlist import RequestPolicy
from .exceptions import CannotConnect, UnexpectedResponse
from .redact import host_path

_LOGGER = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

# Utility-side pages sit behind a CDN/WAF that rejects bare requests.
BROWSER_HEADERS: Mapping[str, str] = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Silverblaze API calls need no cookies.
API_HEADERS: Mapping[str, str] = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
}

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_KEEP_METHOD_STATUSES = frozenset({307, 308})


@dataclass(frozen=True)
class HttpResponse:
    """Final response of a (possibly redirected) request.

    ``url`` is the last URL requested. It carries a query only when a redirect
    ``Location`` supplied one; the caller's ``params`` are not merged into it.
    """

    status: int
    url: str
    text: str
    history: tuple[tuple[int, str], ...] = field(default_factory=tuple)
    location: str | None = None  # Location header; set only when a redirect was not followed


class SilverblazeHttp:
    """All client I/O goes through ``request``; nothing else touches the session."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        policy: RequestPolicy,
        *,
        min_interval: float = 1.0,
        timeout: float = 90,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._policy = policy
        self._min_interval = min_interval
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._clock = clock
        self._last_sent: float | None = None
        self._pace_lock = asyncio.Lock()

    def clear_cookies(self) -> None:
        """Drop all session cookies; used to restart a sign-in from scratch."""
        self._session.cookie_jar.clear()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Any = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        follow_redirects: bool = True,
        max_redirects: int = 10,
        max_retries: int | None = None,
    ) -> HttpResponse:
        """Send a request, manually following redirects with a policy check per hop.

        ``max_retries`` overrides the instance default for this call, redirect hops included.
        """
        method = method.upper()
        history: list[tuple[int, str]] = []
        hops = 0
        while True:
            status, text, location = await self._send_with_retries(
                method, url, params, data, json, headers, max_retries
            )
            _LOGGER.debug("%s %s -> %s", method, host_path(url), status)
            if status in _REDIRECT_STATUSES and location and follow_redirects:
                history.append((status, host_path(url)))
                hops += 1
                if hops > max_redirects:
                    raise UnexpectedResponse("Too many redirects") from None
                url = urljoin(url, location)
                params = None
                if status not in _KEEP_METHOD_STATUSES:
                    method, data, json = "GET", None, None
                continue
            return HttpResponse(status, url, text, tuple(history), location)

    async def _pace(self) -> None:
        async with self._pace_lock:
            if self._last_sent is not None:
                wait = self._last_sent + self._min_interval - self._clock()
                if wait > 0:
                    await self._sleep(wait)
            self._last_sent = self._clock()

    async def _send_with_retries(
        self,
        method: str,
        url: str,
        params: Mapping[str, Any] | None,
        data: Any,
        json: Any,
        headers: Mapping[str, str] | None,
        max_retries: int | None = None,
    ) -> tuple[int, str, str | None]:
        where = f"{method} {host_path(url)}"
        reason = "request failed"
        retries = self._max_retries if max_retries is None else max_retries
        for attempt in range(retries + 1):
            if attempt:
                await self._sleep(self._backoff_base**attempt)
            # Policy check precedes every send, including retries and redirect hops.
            self._policy.check(method, url)
            await self._pace()
            try:
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    json=json,
                    headers=headers,
                    allow_redirects=False,
                    timeout=self._timeout,
                ) as resp:
                    status = resp.status
                    text = await resp.text()
                    location = resp.headers.get("Location")
            except UnicodeDecodeError:
                # The error's repr would embed the body; raise a fixed message instead.
                raise UnexpectedResponse(f"{where}: response body could not be decoded") from None
            except TimeoutError:
                reason = "timed out"
                continue
            except aiohttp.ClientError:
                reason = "connection error"
                continue
            if status >= 500:
                reason = f"server error {status}"
                continue
            return status, text, location
        # ``where`` is a vetted allowlisted host/path plus fixed text: no redaction needed.
        raise CannotConnect(f"{where} failed after retries: {reason}") from None
