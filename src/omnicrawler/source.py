"""Retrieve authorized data through an external tool we do not govern.

An adapter answers exactly two questions for a run: which identities belong to
the authorized scope, and what each identity says right now. Everything after
that — evidence contracts, snapshot comparison, change inference, reports — is
the same code the controlled HTTP collector uses, written through the same
:func:`omnicrawler.pipeline.save_run`.

Boundary disclosure is the point of this module. Retrieval happens inside the
external tool, so the allowlist, robots check, DNS re-validation, byte budget and
per-host spacing in ``transport`` do not apply to it. Every adapter run carries an
``external_retrieval`` deviation and names the adapter in ``engine``, so a reader
never mistakes it for a run those controls governed. This path is for data the
operator is authorized to read — for the Xiaohongshu adapter, the operator's own
account.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from omnicrawler.governance import sanitize
from omnicrawler.models import CollectError, Contract, Coverage, Evidence, Record
from omnicrawler.pipeline import CollectionResult, save_run

EXTERNAL_RETRIEVAL = CollectError(
    code="external_retrieval",
    message=(
        "Retrieved by an external adapter; the HTTP allowlist, robots check, DNS "
        "re-validation and rate limiting were not applied to this run"
    ),
)


class SourceConfig(Contract):
    """Authorized scope and field mapping for one external adapter run.

    ``fields`` maps a field name to one or more paths inside the adapter's own
    payload (a dotted key path, with ``[]`` to map over a list). Nothing here is a
    CSS selector: the adapter resolves the paths, because only it knows the shape
    of what it fetched.
    """

    kind: str = Field(min_length=1)
    account: str = Field(min_length=1)
    goal: str = ""
    endpoint: str | None = None
    limit: int = Field(default=20, gt=0, le=200)
    fields: dict[str, str | list[str]] = Field(min_length=1)

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in value)
        ):
            raise ValueError("Adapter endpoint must be a credential-free HTTP(S) URL")
        if parsed.scheme == "http" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("Adapter endpoint requires HTTPS or an explicit loopback")
        return value

    @field_validator("fields")
    @classmethod
    def fields_have_paths(cls, value):
        for name, paths in value.items():
            if not name.strip():
                raise ValueError("Field names must be non-empty")
            candidates = [paths] if isinstance(paths, str) else list(paths)
            if not candidates or any(not str(path).strip() for path in candidates):
                raise ValueError(f"Field {name!r} needs at least one payload path")
        return value


class SourceAdapter(Protocol):
    """What a retriever must provide to be driven by :func:`collect_from_source`."""

    name: str

    async def identities(self) -> list[dict]:
        """Every identity in the authorized scope.

        Must raise on failure rather than returning an empty list: for a monitor
        "nothing found" and "the tool broke" are indistinguishable downstream, and
        guessing wrong reports a mass deletion.
        """

    async def observe(self, entry: dict) -> dict:
        """Current values plus per-field provenance for one identity."""


def source_coverage(config: SourceConfig) -> str:
    """Scope identity for an adapter run.

    The resolved identity list is deliberately excluded: for a monitor the item
    set is *data*, so a newly published item must register as an addition rather
    than silently reset the baseline. The session token is excluded as well, so
    rotating it cannot fork the history.
    """
    return hashlib.sha256(
        json.dumps(
            {
                "kind": config.kind,
                "account": config.account,
                "endpoint": config.endpoint,
                "limit": config.limit,
                "fields": config.fields,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _captured_at(value) -> datetime:
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed
    return datetime.now(UTC)


def _adapter_error(exc: BaseException, config: SourceConfig, url=None) -> CollectError:
    message = (str(exc) or type(exc).__name__)[:500]
    lowered = message.lower()
    if any(
        hint in lowered
        for hint in ("login", "cookie", "未登录", "登录", "unauthor", "credential")
    ):
        code, retryable = "source_not_authenticated", False
    elif isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        code, retryable = "source_timeout", True
    else:
        code, retryable = "source_unavailable", True
    return CollectError(
        code=code, message=message, url=url or config.endpoint, retryable=retryable
    )


async def collect_from_source(config, store, adapter) -> CollectionResult:
    """One adapter run, recorded with the same evidence and comparison semantics."""
    errors: list[CollectError] = []
    warnings: list[CollectError] = [EXTERNAL_RETRIEVAL]
    coverage = source_coverage(config)
    previous = store.latest(coverage)
    try:
        entries = list(await adapter.identities())
    except Exception as exc:  # noqa: BLE001 — task boundary records adapter failure
        entries = []
        errors.append(_adapter_error(exc, config))
    truncated = len(entries) > config.limit
    if truncated:
        # Same rule as the page budget: a partial scope cannot justify a deletion.
        errors.append(
            CollectError(
                code="source_scope_truncated",
                message=f"Adapter reported {len(entries)} identities, the configured "
                f"limit is {config.limit}; coverage is partial",
                url=config.endpoint,
            )
        )
    records: dict[str, Record] = {}
    observations: list[Record] = []
    fetches: list[dict] = []
    unresolved: list[str] = []
    misses = dict.fromkeys(config.fields, 0)
    for entry in entries[: config.limit]:
        identity = str(entry.get("identity") or "").strip()
        if not identity:
            errors.append(
                CollectError(
                    code="entity_identity_missing",
                    message="Adapter returned an entry without a stable identity; "
                    "the entity universe is incomplete",
                    url=config.endpoint,
                )
            )
            continue
        try:
            observed = await adapter.observe(entry)
        except Exception as exc:  # noqa: BLE001 — task boundary records adapter failure
            errors.append(_adapter_error(exc, config, identity))
            continue
        captured = _captured_at(observed.get("observed_at"))
        url = str(observed.get("url") or "")
        values = observed.get("values") or {}
        provenance = observed.get("provenance") or {}
        fetches.append(
            {
                "url": url or None,
                "requested_url": identity,
                "captured_at": captured.isoformat(),
                "engine": f"source:{adapter.name}",
                "status": 200,
            }
        )
        fields, evidence = {}, {}
        for name in config.fields:
            value = values.get(name)
            proof = provenance.get(name) or {}
            proof_url = str(proof.get("url") or url)
            if value is None or not str(value).strip() or not proof_url:
                # A field we cannot both read and point back to is not a field:
                # the evidence contract is not negotiable on this path either.
                misses[name] += 1
                continue
            text = sanitize(str(value))
            if not text.strip():
                misses[name] += 1
                continue
            fields[name] = text
            evidence[name] = Evidence(
                url=proof_url,
                captured_at=captured,
                locator=str(
                    proof.get("locator") or f"{adapter.name}:{identity}#{name}"
                ),
                excerpt=text[:2000],
            )
        if not fields:
            unresolved.append(identity)
            continue
        record = Record(entity_id=identity, fields=fields, evidence=evidence)
        observations.append(record)
        if identity in records and records[identity].fields != fields:
            errors.append(
                CollectError(
                    code="conflicting_entity_values",
                    message=f"Identity {identity!r} was observed with conflicting "
                    "values in one run; snapshot is not comparable",
                    url=url or config.endpoint,
                )
            )
            continue
        records[identity] = record
    if not entries and not errors:
        errors.append(
            CollectError(
                code="source_empty_scope",
                message="The adapter reported no identities; an empty scope cannot "
                "be told apart from a failed enumeration, so no deletion is inferred",
                url=config.endpoint,
            )
        )
    regressed = sorted(
        key for key in unresolved if previous is not None and key in previous.records
    )
    if regressed:
        errors.append(
            CollectError(
                code="entity_unresolved",
                message=f"{len(regressed)} previously recorded identit(ies) no longer "
                "resolved a field: " + ", ".join(regressed[:5]),
                url=config.endpoint,
            )
        )
    for name, count in misses.items():
        if count:
            warnings.append(
                CollectError(
                    code="field_missing",
                    message=f"Field {name!r} was unresolved on {count} item(s); the "
                    "remaining fields were kept",
                    url=config.endpoint,
                )
            )
    detail = Coverage(
        planned_pages=len(entries),
        fetched_pages=len(fetches),
        truncated=truncated,
        complete=not errors,
        warnings=warnings,
    )
    engine = f"source:{adapter.name}"
    snap = save_run(
        store,
        coverage=coverage,
        detail=detail,
        records=records,
        fetches=fetches,
        observations=observations,
        errors=errors,
        warnings=warnings,
        engine=engine,
        goal=config.goal or config.account,
        route_reason=f"external source adapter ({adapter.name}); network policy not applied",
    )
    changes = store.compare(previous.id, snap.id) if previous is not None else ()
    return CollectionResult(
        status="partial" if errors else "succeeded",
        snapshot_id=snap.id,
        previous_snapshot_id=previous.id if previous else None,
        changes=list(changes),
        errors=errors,
        warnings=warnings,
        coverage=detail,
        engine=engine,
    )
