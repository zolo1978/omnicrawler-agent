"""Collection orchestration with deterministic extraction and durable evidence."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnicrawler.core import Change, Snapshot, make_plan
from omnicrawler.governance import assess, sanitize
from omnicrawler.models import Evidence, Record
from omnicrawler.policy import AccessDenied
from omnicrawler.transport import SafeFetcher


class CollectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = "Collect structured records and monitor changes"
    discovery: Literal["none", "links", "sitemap", "rss"] = "none"
    empty_selector: str | None = None
    urls: list[str] = Field(min_length=1)
    allowed_hosts: list[str] = Field(min_length=1)
    local_origins: list[str] = Field(default_factory=list)
    item_selector: str = Field(min_length=1)
    id_attribute: str = Field(min_length=1)
    fields: dict[str, str] = Field(min_length=1)
    engine: Literal["auto", "html", "playwright", "crawl4ai"] = "auto"
    ready_selector: str | None = None
    max_pages: int = Field(default=100, gt=0, le=1000, strict=True)
    delay: float = Field(default=1, ge=0)

    @model_validator(mode="after")
    def validate_plan(self):
        make_plan(self.urls, list(self.fields), self.max_pages)
        if len(set(self.urls)) > self.max_pages:
            raise ValueError("Seed URLs exceed page budget")
        if set(self.fields) & {
            "email",
            "phone",
            "username",
            "password",
            "cookie",
            "address",
        }:
            raise ValueError("Personal or credential fields are disabled by default")
        return self


class CollectionResult(BaseModel):
    status: str
    snapshot_id: str
    previous_snapshot_id: str | None = None
    changes: list[Change]
    errors: list[str]


def select_engine(config: CollectionConfig) -> str:
    if config.engine != "auto":
        return config.engine
    if config.ready_selector:
        return "playwright"
    from importlib.util import find_spec

    return "crawl4ai" if find_spec("crawl4ai") is not None else "html"


async def collect(config: CollectionConfig, store) -> CollectionResult:
    selected_engine = select_engine(config)
    plan = make_plan(config.urls, list(config.fields), config.max_pages)
    coverage = hashlib.sha256(
        json.dumps(
            {
                "urls": sorted(plan.urls),
                "selector": config.item_selector,
                "id": config.id_attribute,
                "fields": config.fields,
                "engine": selected_engine,
                "discovery": config.discovery,
                "budget": config.max_pages,
                "empty": config.empty_selector,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    previous = store.latest(coverage)
    client = SafeFetcher(
        config.allowed_hosts, local_origins=config.local_origins, delay=config.delay
    )
    records, errors, fetches = {}, [], []
    observations = []
    blocked = False
    urls = list(plan.urls)
    if config.discovery != "none":
        from omnicrawler.discovery import extract_sources

        urls = []
        for seed in plan.urls:
            try:
                source = await client.fetch(seed)
                candidates = extract_sources(
                    source.html, source.url, config.discovery, set(config.allowed_hosts)
                )
                urls.extend(
                    candidate for candidate in candidates if candidate not in urls
                )
            except AccessDenied:
                blocked = True
                errors.append("access_denied")
                break
            except Exception as exc:  # noqa: BLE001 — task boundary records unknown adapter failure safely
                errors.append(type(exc).__name__)
        if len(urls) > config.max_pages:
            errors.append("discovery_truncated")
            urls = urls[: config.max_pages]
        if not urls:
            errors.append("no_sources_discovered")
    for url in [] if blocked else urls:
        try:
            page = await client.fetch(url)
            html = page.html
            if selected_engine != "html":
                from omnicrawler.engines import transform

                html = await transform(
                    selected_engine, html, page.url, config.ready_selector
                )
            captured = datetime.now(UTC)
            fetches.append(
                {
                    "url": page.url,
                    "requested_url": url,
                    "captured_at": captured.isoformat(),
                    "engine": selected_engine,
                    "status": page.status,
                    "hash": hashlib.sha256(html.encode()).hexdigest(),
                }
            )
            soup = BeautifulSoup(
                page.html if selected_engine == "crawl4ai" else html, "html.parser"
            )
            items = soup.select(config.item_selector)
            if not items and not (
                config.empty_selector and soup.select_one(config.empty_selector)
            ):
                raise ValueError(
                    "Item selector matched no records; deletion requires explicit empty-state support"
                )
            for item in items:
                key = item.get(config.id_attribute)
                if not isinstance(key, str) or not key.strip():
                    raise ValueError("Item missing stable entity identity")
                fields, evidence = {}, {}
                for name, selector in config.fields.items():
                    node = item.select_one(selector)
                    if node is None or not node.get_text(strip=True):
                        raise ValueError("Required field missing")
                    fields[name] = sanitize(node.get_text(" ", strip=True))
                    evidence[name] = Evidence(
                        url=page.url,
                        captured_at=captured,
                        locator=f"{config.item_selector}[{config.id_attribute}={key}] {selector}",
                        excerpt=fields[name][:2000],
                    )
                record = Record(entity_id=key, fields=fields, evidence=evidence)
                observations.append(record)
                if key in records and records[key].fields != fields:
                    raise ValueError("Conflicting entity values; snapshot incomplete")
                records[key] = record
        except AccessDenied:
            errors.append("access_denied")
            blocked = True
            break  # Refusal stops task, never switch engines.
        except Exception as exc:  # noqa: BLE001 — task boundary records unknown adapter failure safely
            errors.append(
                type(exc).__name__
            )  # Do not expose source contents or credentials.
    identifier = str(uuid4())
    snap = Snapshot(
        identifier,
        coverage,
        {key: record.fields for key, record in records.items()},
        not errors,
    )
    bundle = {
        "snapshot_id": identifier,
        "coverage": coverage,
        "complete": snap.complete,
        "created_at": datetime.now(UTC).isoformat(),
        "records": [record.model_dump(mode="json") for record in records.values()],
        "fetches": fetches,
        "claims": assess(observations),
        "goal": config.goal,
        "route_reason": {
            "html": "deterministic HTML extraction",
            "playwright": "explicit inline-script rendering",
            "crawl4ai": "explicit Crawl4AI content parsing",
        }[selected_engine],
    }
    store.save_bundle(snap, bundle)
    changes = store.compare(previous.id, identifier) if previous is not None else ()
    status = "blocked" if blocked else "partial" if errors else "succeeded"
    return CollectionResult(
        status=status,
        snapshot_id=identifier,
        previous_snapshot_id=previous.id if previous else None,
        changes=list(changes),
        errors=errors,
    )
