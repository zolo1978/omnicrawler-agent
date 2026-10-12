"""Nominate candidate sources from a user-configured search backend.

Search is a *nomination* channel, never an authorization one. The backend is
trusted only to suggest URLs; every suggestion still has to pass the same
allowlist filter as :func:`omnicrawler.discovery.extract_sources` before it can
reach a caller. A compromised or over-eager backend therefore cannot widen a
run's scope, and an unconfigured backend leaves the deterministic path
untouched.
"""

import json
import os
from urllib.parse import urlsplit

import aiohttp

from omnicrawler.models import CollectError, SearchConfig

TIMEOUT = 30
MAX_BYTES = 1_000_000


def search_from_env() -> SearchConfig | None:
    """Configured nomination backend, or ``None`` when the operator opted out."""
    endpoint = os.environ.get("OMNICRAWLER_SEARCH_URL")
    token = os.environ.get("OMNICRAWLER_SEARCH_TOKEN")
    if not endpoint and not token:
        return None
    if not endpoint:
        raise ValueError("Set OMNICRAWLER_SEARCH_URL to enable source nomination")
    return SearchConfig(endpoint=endpoint, token=token)


def _query_for(config: SearchConfig, goal: str) -> str:
    if not goal.strip():
        raise ValueError("Source nomination requires a goal")
    try:
        return config.query_template.format(goal=goal)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            "Search query_template may only use the {goal} placeholder"
        ) from exc


def _nominate(candidates, allowed_hosts: set[str], max_results: int):
    """Split raw results into authorized nominations and operator-facing rejections.

    Rejections are returned rather than dropped so an over-scoped backend is
    visible in the session instead of silently shrinking the candidate list.
    """
    accepted: list[str] = []
    rejected: list[CollectError] = []
    for target in candidates:
        parsed = urlsplit(target)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in allowed_hosts
            and parsed.username is None
            and parsed.password is None
        ):
            if target not in accepted:
                accepted.append(target)
        else:
            rejected.append(
                CollectError(
                    code="search_candidate_rejected",
                    message="Candidate is outside the authorized HTTP(S) allowlist",
                    url=target,
                )
            )
    for extra in accepted[max_results:]:
        rejected.append(
            CollectError(
                code="search_candidate_truncated",
                message="Candidate exceeds the configured max_results budget",
                url=extra,
            )
        )
    return accepted[:max_results], rejected


async def _fetch_results(config: SearchConfig, query: str) -> list[str]:
    """One bounded GET against a SearXNG-compatible JSON search endpoint."""
    if config.endpoint is None:
        raise ValueError("Search config requires an endpoint")
    headers = {"Authorization": f"Bearer {config.token}"} if config.token else {}
    async with (
        aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=TIMEOUT)
        ) as session,
        session.get(
            f"{config.endpoint.rstrip('/')}/search",
            params={"q": query, "format": "json"},
            headers=headers,
            allow_redirects=False,
        ) as response,
    ):
        if response.status != 200:
            raise ValueError(
                "Search backend refused or failed; no fallback backend attempted"
            )
        raw = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise ValueError("Search response exceeds budget")
    try:
        payload = json.loads(raw)
        results = payload["results"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(
            "Search backend did not return the expected JSON envelope"
        ) from exc
    if not isinstance(results, list):
        raise TypeError("Search backend returned a non-list results field")
    urls: list[str] = []
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise TypeError("Search result entry is missing a URL string")
        urls.append(item["url"])
    return urls


async def search_candidates(
    *, goal: str, config: SearchConfig | None, allowed_hosts: set[str]
) -> tuple[list[str], list[CollectError]]:
    """Authorized nominations plus the rejections an operator must be told about."""
    if config is None:
        raise ValueError(
            "Source nomination requires a configured search backend; provide "
            "source URLs explicitly or set OMNICRAWLER_SEARCH_URL"
        )
    accepted, rejected = _nominate(
        await _fetch_results(config, _query_for(config, goal)),
        allowed_hosts,
        config.max_results,
    )
    if not accepted:
        raise ValueError(
            "Search backend nominated no source inside the authorized allowlist"
            + (f"; {len(rejected)} candidate(s) rejected" if rejected else "")
        )
    return accepted, rejected


async def search_sources(
    *, goal: str, config: SearchConfig | None, allowed_hosts: set[str]
) -> list[str]:
    """Pipeline entry point: authorized nominations, or a clear typed failure.

    Never returns an empty list, because an unusable backend must surface as an
    error record instead of a silent "no sources discovered".
    """
    accepted, _ = await search_candidates(
        goal=goal, config=config, allowed_hosts=allowed_hosts
    )
    return accepted
