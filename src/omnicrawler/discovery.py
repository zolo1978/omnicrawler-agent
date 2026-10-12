"""Discover candidate URLs; every actual fetch still passes transport policy.

In-page discovery lives here; backend *nomination* lives in
:mod:`omnicrawler.search`, re-exported below so every discovery mode has one
entry point. Both paths only propose — the allowlist still decides.
"""

from urllib.parse import urldefrag, urljoin, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from omnicrawler.models import SearchConfig


async def search_sources(
    *, goal: str, config: SearchConfig | None, allowed_hosts: set[str]
) -> list[str]:
    """Delegate to :mod:`omnicrawler.search` without importing aiohttp eagerly."""
    from omnicrawler.search import search_sources as nominate

    return await nominate(goal=goal, config=config, allowed_hosts=allowed_hosts)


def extract_sources(
    content: str, base: str, mode: str, allowed_hosts: set[str]
) -> list[str]:
    if mode == "links":
        candidates = [
            node.get("href", "")
            for node in BeautifulSoup(content, "html.parser").select("a[href]")
        ]
    elif mode in {"rss", "sitemap"}:
        root = ElementTree.fromstring(content)
        candidates = [
            node.text or ""
            for node in root.iter()
            if node.tag.rsplit("}", 1)[-1]
            in ({"loc"} if mode == "sitemap" else {"link"})
        ]
    else:
        raise ValueError("Unknown discovery mode")
    output = []
    for candidate in candidates:
        target = urldefrag(urljoin(base, candidate.strip()))[0]
        parsed = urlsplit(target)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in allowed_hosts
            and parsed.username is None
            and parsed.password is None
            and target not in output
        ):
            output.append(target)
    return output
