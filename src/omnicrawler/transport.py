"""Bounded HTTP transport with validated, connection-pinned DNS resolution."""

import asyncio
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from omnicrawler.policy import AccessDenied, AccessPolicy


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
    ):
        self.policy = AccessPolicy(set(allowed_hosts))
        self.local_origins = set(local_origins)
        self.delay = delay
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.last = {}
        self.scheme = "https"

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
            if response.status in {401, 403, 429}:
                raise AccessDenied("Source refused access")
            payload = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                payload.extend(chunk)
                if len(payload) > self.max_bytes:
                    raise ValueError("Response exceeded byte budget")
            return (
                response.status,
                dict(response.headers),
                payload.decode(response.charset or "utf-8", errors="replace"),
            )

    async def robots(self, url):
        parsed = urlsplit(url)
        endpoint = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        status, _, body = await self.request(endpoint)
        if status == 404:
            return ""
        if status != 200:
            raise AccessDenied("robots.txt unavailable or refused")
        return body

    async def fetch(self, url):
        for _ in range(6):
            rules = await self.robots(url)
            if not self.policy.robots_allowed(url, rules):
                raise AccessDenied("robots.txt denies this path")
            status, headers, html = await self.request(url)
            if status in {401, 403, 429}:
                raise AccessDenied("Source refused access")
            if status in {301, 302, 303, 307, 308}:
                location = headers.get("Location")
                if not location:
                    raise ValueError("Redirect without Location")
                url = urljoin(url, location)
                continue
            if status != 200:
                raise ValueError(f"HTTP status {status}")
            return Page(url, status, html)
        raise ValueError("Redirect budget exceeded")
