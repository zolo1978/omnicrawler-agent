"""Collection orchestration with deterministic extraction and durable evidence.

Two deviation classes, deliberately asymmetric (RE01):

* Entity-universe deviations — unreadable item identity, two items claiming the
  same identity with different values, or an item selector matching nothing
  without an explicit empty-state marker — record an error and make the snapshot
  incomplete, so no change can be inferred from it.
* Field-level deviations — a field selector missing on some items — record a
  warning and keep the snapshot comparable. One renamed class must not zero a
  page or silently disable change monitoring.
"""

import hashlib
import json
from datetime import UTC, datetime
from importlib.util import find_spec
from urllib.parse import urljoin
from uuid import uuid4

import soupsieve
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnicrawler.core import Change, Snapshot, make_plan
from omnicrawler.governance import assess, sanitize
from omnicrawler.models import CollectError, Coverage, Evidence, Record, SearchConfig
from omnicrawler.policy import AccessDenied
from omnicrawler.transport import SafeFetcher

DISABLED_FIELDS = {"email", "phone", "username", "password", "cookie", "address"}


def selectors_for(value) -> tuple[str, ...]:
    """A field may declare an ordered fallback chain of CSS selectors."""
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _compile(selector: str, label: str) -> str:
    try:
        soupsieve.compile(selector)
    except Exception as exc:
        raise ValueError(f"Invalid {label} {selector!r}: {exc}") from exc
    return selector


class Pagination(BaseModel):
    """Bounded page following, applied per seed URL within the page budget."""

    model_config = ConfigDict(extra="forbid")

    next_selector: str | None = None
    url_template: str | None = None
    max_pages: int = Field(default=1, gt=0, le=100)

    @model_validator(mode="after")
    def validate_mode(self):
        if bool(self.next_selector) == bool(self.url_template):
            raise ValueError(
                "Pagination requires exactly one of next_selector or url_template"
            )
        if self.url_template and "{page}" not in self.url_template:
            raise ValueError("url_template must contain the {page} placeholder")
        return self


class CollectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = "Collect structured records and monitor changes"
    discovery: str = "none"
    empty_selector: str | None = None
    urls: list[str] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(min_length=1)
    local_origins: list[str] = Field(default_factory=list)
    item_selector: str = Field(min_length=1)
    id_attribute: str = Field(min_length=1)
    fields: dict[str, str | list[str]] = Field(min_length=1)
    engine: str = "auto"
    ready_selector: str | None = None
    pagination: Pagination | None = None
    search: SearchConfig | None = None
    store_raw_html: bool = False
    max_pages: int = Field(default=100, gt=0, le=1000, strict=True)
    delay: float = Field(default=1, ge=0)
    timeout: float = Field(default=20, gt=0)
    max_bytes: int = Field(default=2_000_000, gt=0)
    retries: int = Field(default=2, ge=0, le=5)

    @model_validator(mode="after")
    def validate_plan(self):
        if self.discovery not in {"none", "links", "sitemap", "rss", "search"}:
            raise ValueError("Unknown discovery mode")
        if self.discovery == "search":
            if self.search is None:
                raise ValueError("discovery 'search' requires search settings")
        elif not self.urls:
            raise ValueError("URLs are required unless discovery is 'search'")
        if self.engine not in {"auto", "html", "playwright", "crawl4ai"}:
            raise ValueError("Unknown content engine")
        if not self.fields or any(not name.strip() for name in self.fields):
            raise ValueError("Non-empty field names are required")
        if len(set(self.urls)) > self.max_pages:
            raise ValueError("Seed URLs exceed page budget")
        if set(self.fields) & DISABLED_FIELDS:
            raise ValueError("Personal or credential fields are disabled by default")
        if self.urls:
            make_plan(self.urls, list(self.fields), self.max_pages)
        _compile(self.item_selector, "item_selector")
        if self.empty_selector:
            _compile(self.empty_selector, "empty_selector")
        if self.ready_selector:
            _compile(self.ready_selector, "ready_selector")
        for name, selectors in self.fields.items():
            if not selectors_for(selectors):
                raise ValueError(f"Field {name!r} needs at least one selector")
            for selector in selectors_for(selectors):
                _compile(selector, f"selector for field {name!r}")
        if self.pagination is not None and self.pagination.next_selector:
            _compile(self.pagination.next_selector, "pagination.next_selector")
        return self


class CollectionResult(BaseModel):
    status: str
    snapshot_id: str
    previous_snapshot_id: str | None = None
    changes: list[Change]
    errors: list[CollectError]
    warnings: list[CollectError] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)
    engine: str = "html"


def engine_available(engine: str) -> bool:
    """Whether a pinned engine can run here; a silent switch would change results."""
    if engine == "crawl4ai":
        return find_spec("crawl4ai") is not None
    return engine in {"html", "playwright"}


def select_engine(config: CollectionConfig) -> str:
    if config.engine != "auto":
        return config.engine
    if config.ready_selector:
        return "playwright"
    return "crawl4ai" if find_spec("crawl4ai") is not None else "html"


def coverage_id(config: CollectionConfig, resolved_urls) -> str:
    """Scope identity. The engine is deliberately excluded and pinned separately."""
    return hashlib.sha256(
        json.dumps(
            {
                "urls": sorted(config.urls),
                "resolved": sorted(resolved_urls),
                "selector": config.item_selector,
                "id": config.id_attribute,
                "fields": config.fields,
                "discovery": config.discovery,
                "search": config.search.model_dump(mode="json")
                if config.search
                else None,
                "budget": config.max_pages,
                "empty": config.empty_selector,
                "pagination": config.pagination.model_dump(mode="json")
                if config.pagination
                else None,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _error(code, message, url=None, retryable=False) -> CollectError:
    return CollectError(code=code, message=message, url=url, retryable=retryable)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    return "Client" in type(exc).__name__ or "Timeout" in type(exc).__name__


def _fetch_error(exc: BaseException, url: str) -> CollectError:
    message = str(exc) or type(exc).__name__
    if message.startswith("HTTP status"):
        return _error("http_status", message, url, retryable=False)
    return _error(type(exc).__name__, message, url, _retryable(exc))


def next_page_url(
    pagination: Pagination, current_url: str, html: str, page: int
) -> str | None:
    """One page forward, or None at the end of the sequence."""
    if pagination.url_template:
        return pagination.url_template.format(page=page + 1)
    node = BeautifulSoup(html, "html.parser").select_one(pagination.next_selector)
    if node is None:
        return None
    href = node.get("href")
    if not isinstance(href, str) or not href.strip():
        return None
    return urljoin(current_url, href.strip())


async def _resolve_sources(config, client) -> tuple[list[str], list[CollectError]]:
    """Seed expansion for discovery modes. Search nominates; the allowlist decides."""
    errors: list[CollectError] = []
    if config.discovery == "search":
        from omnicrawler.discovery import search_sources

        try:
            return (
                await search_sources(
                    goal=config.goal,
                    config=config.search,
                    allowed_hosts=set(config.allowed_hosts),
                ),
                errors,
            )
        except Exception as exc:  # noqa: BLE001 — task boundary records adapter failure
            errors.append(_fetch_error(exc, config.search.endpoint or "search"))
            return [], errors
    if config.discovery == "none":
        return list(config.urls), errors
    from omnicrawler.discovery import extract_sources

    urls: list[str] = []
    for seed in config.urls:
        try:
            source = await client.fetch(seed)
            candidates = extract_sources(
                source.html, source.url, config.discovery, set(config.allowed_hosts)
            )
            urls.extend(candidate for candidate in candidates if candidate not in urls)
        except AccessDenied as exc:
            errors.append(_error("access_denied", str(exc), seed))
            break
        except Exception as exc:  # noqa: BLE001 — task boundary records adapter failure
            errors.append(_fetch_error(exc, seed))
    if len(urls) > config.max_pages:
        errors.append(
            _error(
                "discovery_truncated",
                f"Discovered {len(urls)} links, page budget is {config.max_pages}",
                config.urls[0] if config.urls else None,
            )
        )
        urls = urls[: config.max_pages]
    if not urls:
        errors.append(
            _error("no_sources_discovered", "Discovery produced no allowed URLs")
        )
    return urls, errors


def save_run(
    store,
    *,
    coverage: str,
    detail: Coverage,
    records: dict[str, Record],
    fetches: list[dict],
    observations: list[Record],
    errors: list[CollectError],
    warnings: list[CollectError],
    engine: str,
    goal: str,
    route_reason: str,
    raw_html_stored: bool = False,
) -> Snapshot:
    """Persist one run's snapshot and evidence bundle.

    Every retriever (the controlled HTTP collector and an external source
    adapter) writes through here, so the bundle keys the report and the
    synthesis layer read can never drift between paths.
    """
    identifier = str(uuid4())
    snap = Snapshot(
        identifier,
        coverage,
        {key: record.fields for key, record in records.items()},
        detail.complete,
        detail.model_dump(mode="json"),
    )
    bundle = {
        "snapshot_id": identifier,
        "coverage": coverage,
        "coverage_detail": detail.model_dump(mode="json"),
        "complete": snap.complete,
        "created_at": datetime.now(UTC).isoformat(),
        "records": [record.model_dump(mode="json") for record in records.values()],
        "fetches": fetches,
        "errors": [error.model_dump(mode="json") for error in errors],
        "warnings": [warning.model_dump(mode="json") for warning in warnings],
        "raw_html_stored": raw_html_stored,
        "claims": assess(observations),
        "goal": goal,
        "engine": engine,
        "route_reason": route_reason,
    }
    store.save_bundle(snap, bundle)
    return snap


async def collect(config: CollectionConfig, store) -> CollectionResult:
    errors: list[CollectError] = []
    warnings: list[CollectError] = []
    records: dict[str, Record] = {}
    observations = []
    fetches: list[dict] = []
    blocked = False
    truncated = False
    fetched = 0
    client = SafeFetcher(
        config.allowed_hosts,
        local_origins=config.local_origins,
        delay=config.delay,
        timeout=config.timeout,
        max_bytes=config.max_bytes,
        retries=config.retries,
    )
    urls, discovery_errors = await _resolve_sources(config, client)
    errors.extend(discovery_errors)
    blocked = any(error.code == "access_denied" for error in discovery_errors)
    coverage = coverage_id(config, urls)
    previous = store.latest(coverage)
    pinned = store.engine_for(coverage)
    if pinned is not None and not engine_available(pinned):
        selected_engine = pinned
        blocked = True
        errors.append(
            _error(
                "engine_unavailable",
                f"Pinned engine {pinned!r} is unavailable; refusing to switch engines silently",
            )
        )
    else:
        selected_engine = pinned or select_engine(config)
        if pinned is None:
            selected_engine = store.pin_engine(coverage, selected_engine)
    work = [(url, 1) for url in urls]
    planned = len(work)
    seen = set()
    while work and not blocked:
        if fetched >= config.max_pages:
            truncated = True
            errors.append(
                _error(
                    "page_budget_exceeded",
                    f"Stopped at the {config.max_pages} page budget; coverage is partial",
                    work[0][0],
                )
            )
            break
        url, page_index = work.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            page = await client.fetch(url)
        except AccessDenied as exc:
            errors.append(_error("access_denied", str(exc), url))
            blocked = True
            break  # Refusal stops task, never switch engines.
        except Exception as exc:  # noqa: BLE001 — task boundary records adapter failure
            errors.append(_fetch_error(exc, url))
            continue
        fetched += 1
        captured = datetime.now(UTC)
        if page.lossy:
            warnings.append(
                _error(
                    "text_decoding_lossy",
                    "Response bytes matched no declared, BOM or in-document charset; "
                    "text may contain replacement characters",
                    page.url,
                )
            )
        extracted = page.html
        if selected_engine != "html":
            from omnicrawler.engines import transform

            try:
                extracted = await transform(
                    selected_engine, page.html, page.url, config.ready_selector
                )
            except Exception as exc:  # noqa: BLE001 — task boundary records adapter failure
                errors.append(
                    _error(
                        "engine_failed",
                        f"{type(exc).__name__}: {exc}",
                        page.url,
                        retryable=False,
                    )
                )
                continue
        fetches.append(
            {
                "url": page.url,
                "requested_url": url,
                "captured_at": captured.isoformat(),
                "engine": selected_engine,
                "status": page.status,
                "hash": hashlib.sha256(page.html.encode()).hexdigest(),
                "extracted_hash": hashlib.sha256(extracted.encode()).hexdigest(),
            }
        )
        if config.store_raw_html:
            store.save_document(
                digest=fetches[-1]["hash"],
                url=page.url,
                captured_at=captured.isoformat(),
                html=page.html,
            )
        # Crawl4AI validates the prefetched content; field extraction stays on raw HTML
        # so attribute positions and evidence locators remain derivable.
        soup = BeautifulSoup(
            page.html if selected_engine == "crawl4ai" else extracted, "html.parser"
        )
        items = soup.select(config.item_selector)
        if not items and not (
            config.empty_selector and soup.select_one(config.empty_selector)
        ):
            errors.append(
                _error(
                    "item_selector_no_match",
                    f"Selector {config.item_selector!r} matched no records and no empty-state marker was found",
                    page.url,
                )
            )
            continue
        misses = {name: 0 for name in config.fields}
        unresolved: list[str] = []
        page_records = 0
        for item in items:
            key = item.get(config.id_attribute)
            if not isinstance(key, str) or not key.strip():
                errors.append(
                    _error(
                        "entity_identity_missing",
                        f"Item has no non-empty {config.id_attribute!r} attribute; entity universe is incomplete",
                        page.url,
                    )
                )
                continue
            fields, evidence = {}, {}
            for name, declared in config.fields.items():
                matched = None
                for candidate in selectors_for(declared):
                    found = item.select_one(candidate)
                    if found is None and soupsieve.match(candidate, item):
                        # Descendant-only matching can never reach the item
                        # element's own text, which some listings use as the label.
                        found = item
                    if found is not None and found.get_text(strip=True):
                        matched = (candidate, found)
                        break
                if matched is None:
                    misses[name] += 1
                    continue
                selector, node = matched
                value = sanitize(node.get_text(" ", strip=True))
                fields[name] = value
                evidence[name] = Evidence(
                    url=page.url,
                    captured_at=captured,
                    locator=f"{config.item_selector}[{config.id_attribute}={key}] {selector}",
                    excerpt=value[:2000],
                )
            if not fields:
                unresolved.append(key)
                continue
            record = Record(entity_id=key, fields=fields, evidence=evidence)
            # Conflicts stay visible to assess() so disputed claims keep both values,
            # even though the first observation is the one stored deterministically.
            observations.append(record)
            if key in records and records[key].fields != fields:
                errors.append(
                    _error(
                        "conflicting_entity_values",
                        f"Entity {key!r} appears with conflicting values; snapshot is not comparable",
                        page.url,
                    )
                )
                continue
            is_new = key not in records
            records[key] = record
            if is_new:
                page_records += 1
        if unresolved:
            known = set(previous.records) if previous is not None else set()
            regressed = sorted(key for key in unresolved if key in known)
            if regressed:
                errors.append(
                    _error(
                        "entity_unresolved",
                        f"{len(regressed)} previously recorded entity/entities stopped resolving a field: "
                        + ", ".join(regressed[:5]),
                        page.url,
                    )
                )
        for name, count in misses.items():
            if count:
                warnings.append(
                    _error(
                        "field_selector_no_match",
                        f"Field {name!r} was unresolved on {count} item(s); remaining fields were kept",
                        page.url,
                    )
                )
        if items and page_records == 0:
            errors.append(
                _error(
                    "no_resolvable_records",
                    "Items matched but no complete record could be built; snapshot is not comparable",
                    page.url,
                )
            )
        if config.pagination and page_index < config.pagination.max_pages:
            follow = next_page_url(config.pagination, page.url, page.html, page_index)
            if follow is not None and follow not in seen:
                work.append((follow, page_index + 1))
    coverage_detail = Coverage(
        planned_pages=planned,
        fetched_pages=fetched,
        truncated=truncated,
        complete=not errors and not truncated,
        warnings=warnings,
    )
    snap = save_run(
        store,
        coverage=coverage,
        detail=coverage_detail,
        records=records,
        fetches=fetches,
        observations=observations,
        errors=errors,
        warnings=warnings,
        engine=selected_engine,
        goal=config.goal,
        route_reason={
            "html": "deterministic HTML extraction",
            "playwright": "explicit inline-script rendering",
            "crawl4ai": "explicit Crawl4AI content parsing",
        }[selected_engine],
        raw_html_stored=config.store_raw_html,
    )
    identifier = snap.id
    changes = store.compare(previous.id, identifier) if previous is not None else ()
    status = "blocked" if blocked else "partial" if errors else "succeeded"
    return CollectionResult(
        status=status,
        snapshot_id=identifier,
        previous_snapshot_id=previous.id if previous else None,
        changes=list(changes),
        errors=errors,
        warnings=warnings,
        coverage=coverage_detail,
        engine=selected_engine,
    )
