"""Deterministic planning and snapshot comparison, independent of crawler engines.

URL validation here is structural; access policy and DNS checks run before fetch.
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class TaskPlan:
    urls: tuple[str, ...]
    fields: tuple[str, ...]
    max_pages: int


def make_plan(urls: list[str], fields: list[str], max_pages: int = 100) -> TaskPlan:
    if type(max_pages) is not int or max_pages <= 0:
        raise ValueError("max_pages must be a positive integer")
    if not urls or not fields or any(not field.strip() for field in fields):
        raise ValueError("URLs and nonempty fields are required")
    for url in urls:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Only absolute HTTP(S) URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Credentials must not be embedded in URLs")
        if any(char.isspace() for char in url):
            raise ValueError("URL contains whitespace")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("Invalid URL port") from exc
    return TaskPlan(tuple(dict.fromkeys(urls)), tuple(dict.fromkeys(fields)), max_pages)


@dataclass(frozen=True)
class Snapshot:
    id: str
    coverage: str
    records: dict[str, dict[str, Any]]
    complete: bool


@dataclass(frozen=True)
class Change:
    kind: str
    entity_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None


def detect_changes(old: Snapshot | None, new: Snapshot) -> tuple[Change, ...]:
    """Compare only complete snapshots of the same declared coverage.

    Missing fields are unknown rather than deletions. Transport failures must
    set complete=False. Persistence will deduplicate events by snapshot pair.
    """
    if old is None or old.id == new.id:
        return ()
    if not old.complete or not new.complete or old.coverage != new.coverage:
        return ()
    changes = []
    for entity in sorted(old.records.keys() | new.records.keys()):
        before, after = old.records.get(entity), new.records.get(entity)
        if entity not in old.records:
            changes.append(Change("added", entity, None, dict(after)))
        elif entity not in new.records:
            changes.append(Change("removed", entity, dict(before), None))
        elif any(before[key] != after[key] for key in before.keys() & after.keys()):
            changes.append(Change("modified", entity, dict(before), dict(after)))
    return tuple(changes)
