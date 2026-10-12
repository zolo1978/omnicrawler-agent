"""Bounded HTTP transport with validated, connection-pinned DNS resolution."""

import asyncio
import codecs
import random
import re
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from omnicrawler.policy import AccessDenied, AccessPolicy

# Retry pacing: full jitter keeps concurrent collectors off the same instant, and the
# cap bounds how long one page may stall a serial run before it gives up.
RETRY_BASE_DELAY = 0.25
RETRY_MAX_DELAY = 30.0

_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)
_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.IGNORECASE
)


def decode_body(payload: bytes, charset: str | None) -> tuple[str, bool]:
    """Decode bytes using the best declaration available, and flag lossy results.

    Order: transport charset, then a byte-order mark, then the document's own
    ``<meta charset>``, then UTF-8. Silently replacing undecodable bytes used to turn
    a GBK listing into U+FFFD text that was then stored as field evidence, so the
    caller is told when the result is lossy instead of being left to trust it.
    """
    candidates = []
    if charset:
        candidates.append(charset)
    for bom, name in _BOMS:
        if payload.startswith(bom):
            candidates.insert(0, name)
            break
    match = _META_CHARSET.search(payload[:4096])
    if match:
        candidates.append(match.group(1).decode("ascii", "ignore"))
    candidates.append("utf-8")
    for candidate in candidates:
        try:
            return payload.decode(candidate), False
        except (LookupError, UnicodeDecodeError):
            continue
    fallback = candidates[0] if candidates else "utf-8"
    return payload.decode(fallback, errors="replace"), True


class _Transient(Exception):
    """A response worth retrying: rate limiting (429) or a server fault (5xx).

    Deliberately not an ``AccessDenied``: 401/403 are refusals on sight, while a 429
    only becomes one after the bounded retry budget is spent.
    """

    def __init__(self, status, retry_after=None):
        super().__init__(f"HTTP status {status}")
        self.status = status
        self.retry_after = retry_after


def _parse_retry_after(value):
    """Seconds to wait per RFC 9110 ``Retry-After``: delta-seconds or HTTP-date."""
    if value is None or not value.strip():
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class CheckedResolver(AbstractResolver):
    def __init__(self, owner):
        self.owner = owner

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        results = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM
            ),
            self.owner.timeout,
        )
        addresses = list(dict.fromkeys(row[4][0] for row in results))
        self.owner.check(f"{self.owner.scheme}://{host}:{port}", addresses)
        return [
            {
                "hostname": host,
                "host": row[4][0],
                "port": port,
                "family": row[0],
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
            for row in results
        ]

    async def close(self):
        pass


@dataclass(frozen=True)
class Page:
    url: str
    status: int
    html: str
    lossy: bool = False


class SafeFetcher:
    """One instance per serial collection; do not share across concurrent fetches.

    scheme feeds the connection resolver and last enforces per-host spacing.
    Parallel collection requires per-connection scheme and synchronized throttling.
    """

    def __init__(
        self,
        allowed_hosts,
        *,
        local_origins=(),
        delay=1.0,
        timeout=20,
        max_bytes=2_000_000,
        retries=2,
    ):
        self.policy = AccessPolicy(set(allowed_hosts))
        self.local_origins = set(local_origins)
        self.delay = delay
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.retries = retries
        self.last = {}
        self.scheme = "https"
        self.last_lossy = False

    def check(self, url, addresses):
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self.local_origins:
            # Explicit developer-authorized loopback fixture only, never arbitrary private ranges.
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise AccessDenied("Invalid local fixture origin")
            from ipaddress import ip_address

            if not addresses or not all(ip_address(ip).is_loopback for ip in addresses):
                raise AccessDenied("Fixture DNS is not loopback")
            return
        self.policy.validate_url(url, addresses)

    async def request(self, url):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or any(c.isspace() for c in url)
        ):
            raise AccessDenied("Invalid URL")
        self.scheme = parsed.scheme
        # Also handles literal addresses (aiohttp bypasses resolvers for literals).
        rows = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            ),
            self.timeout,
        )
        self.check(url, list(dict.fromkeys(row[4][0] for row in rows)))
        wait = self.delay - (time.monotonic() - self.last.get(parsed.hostname, 0))
        if wait > 0:
            await asyncio.sleep(wait)
        self.last[parsed.hostname] = time.monotonic()
        connector = aiohttp.TCPConnector(
            resolver=CheckedResolver(self), use_dns_cache=False
        )
        async with (
            aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                headers={"User-Agent": "OmniCrawler/0.1"},
            ) as session,
            session.get(url, allow_redirects=False) as response,
        ):
            if response.status in {401, 403}:
                raise AccessDenied("Source refused access")
            if response.status == 429 or response.status >= 500:
                raise _Transient(response.status, response.headers.get("Retry-After"))
            payload = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                payload.extend(chunk)
                if len(payload) > self.max_bytes:
                    raise ValueError("Response exceeded byte budget")
            body, lossy = decode_body(bytes(payload), response.charset)
            self.last_lossy = lossy
            return (response.status, dict(response.headers), body)

    async def robots(self, url):
        parsed = urlsplit(url)
        endpoint = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        status, _, body = await self.request(endpoint)
        if status == 404:
            return ""
        if status != 200:
            raise AccessDenied("robots.txt unavailable or refused")
        return body

    def _retry_delay(self, attempt, retry_after):
        """Pause before retry ``attempt`` (0-based).

        A stated ``Retry-After`` is authoritative — the source told us when it will
        answer again — and is capped so a bogus far-future date cannot stall a run.
        Otherwise exponential backoff with full jitter.
        """
        stated = _parse_retry_after(retry_after)
        if stated is not None:
            return min(stated, RETRY_MAX_DELAY)
        return min(random.uniform(0, RETRY_BASE_DELAY * 2**attempt), RETRY_MAX_DELAY)

    async def _attempt(self, url):
        """One fetch attempt: robots policy first, then the request and its redirects."""
        for _ in range(6):
            rules = await self.robots(url)
            if not self.policy.robots_allowed(url, rules):
                raise AccessDenied("robots.txt denies this path")
            status, headers, html = await self.request(url)
            if status in {301, 302, 303, 307, 308}:
                location = headers.get("Location")
                if not location:
                    raise ValueError("Redirect without Location")
                url = urljoin(url, location)
                continue
            if status != 200:
                raise ValueError(f"HTTP status {status}")
            return Page(url, status, html, self.last_lossy)
        raise ValueError("Redirect budget exceeded")

    async def fetch(self, url):
        """Fetch a page, retrying only transient failures within a bounded budget.

        Every attempt re-runs DNS validation, per-host spacing and the robots check,
        because a retry must obey exactly the policy the first attempt obeyed. A
        refusal (401/403) is never retried: the source has answered, and the product
        guarantee is that a refusal stops the task rather than being worked around.
        """
        attempt = 0
        while True:
            retry_after = None
            try:
                return await self._attempt(url)
            except AccessDenied:
                raise
            except _Transient as exc:
                if attempt >= self.retries:
                    if exc.status == 429:
                        raise AccessDenied(
                            "Source refused access: rate limited (429) after retries"
                        ) from exc
                    raise ValueError(f"HTTP status {exc.status}") from exc
                retry_after = exc.retry_after
            except (aiohttp.ClientError, TimeoutError, OSError):
                if attempt >= self.retries:
                    raise
            wait = self._retry_delay(attempt, retry_after)
            if wait > 0:
                await asyncio.sleep(wait)
            attempt += 1
