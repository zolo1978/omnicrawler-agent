"""Evidence-gated answer synthesis over one already-collected bundle.

The moat is validation, not prompting. A model conclusion survives only when
every citation it carries matches an ``Evidence`` this run actually stored:
prompts can be argued with, a bundle cannot. Anything unmatched is discarded
rather than repaired or downgraded, so a report never shows a conclusion whose
proof is invisible to the reader.

The rendered text is prose the model authored, so it stays untrusted: callers
must still run it through the report's redaction before publishing.
"""

import json
from dataclasses import asdict, is_dataclass

from pydantic import ValidationError

from omnicrawler import models
from omnicrawler.provider import SynthesisClaim

# Prompt shape is bounded so a wide run cannot push the whole bundle upstream.
_PROMPT_RECORDS = 50
_PROMPT_OBSERVATIONS = 40
_PROMPT_CHANGES = 20
_PROMPT_CHARS = 120_000
_VALUE_LIMIT = 400


def _as_dict(value):
    """``result`` may be a CollectionResult dump, a dataclass, or a stored JSON dict."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return asdict(value) if is_dataclass(value) else dict(value)


def _truncate(value):
    if isinstance(value, dict):
        return {str(key): _truncate(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate(item) for item in value]
    if isinstance(value, str) and len(value) > _VALUE_LIMIT:
        return value[:_VALUE_LIMIT] + "…"
    return value


def _citation_index(bundle) -> dict[tuple[str, str], dict]:
    """The whitelist: ``(url, locator)`` pairs this bundle can prove.

    Built from ``records[*].evidence`` only, because that is the set a reader can
    click back into. Fetches and warnings carry no per-field locator.
    """
    index = {}
    for record in bundle.get("records") or []:
        for proof in (record.get("evidence") or {}).values():
            if isinstance(proof, dict) and proof.get("url") and proof.get("locator"):
                index.setdefault((str(proof["url"]), str(proof["locator"])), proof)
    return index


def _payload(bundle, result, index) -> dict:
    """The model sees the run's own structured findings, never raw HTML."""
    records, size = [], 0
    for record in (bundle.get("records") or [])[:_PROMPT_RECORDS]:
        item = {
            "entity_id": record.get("entity_id"),
            "fields": _truncate(record.get("fields") or {}),
            "evidence": {
                str(field): {
                    "url": proof.get("url"),
                    "locator": proof.get("locator"),
                }
                for field, proof in (record.get("evidence") or {}).items()
                if isinstance(proof, dict)
            },
        }
        size += len(json.dumps(item, ensure_ascii=False))
        if records and size > _PROMPT_CHARS:
            break
        records.append(item)
    changes = []
    for change in (result.get("changes") or [])[:_PROMPT_CHANGES]:
        item = _as_dict(change)
        changes.append(
            {key: _truncate(value) for key, value in item.items() if key != "evidence"}
        )
    observations = []
    for claim in (bundle.get("claims") or [])[:_PROMPT_OBSERVATIONS]:
        observations.append(
            {
                "entity_id": claim.get("entity_id"),
                "field": claim.get("field"),
                "status": claim.get("status"),
                "observations": [
                    {
                        "value": _truncate(observation.get("value")),
                        "url": (observation.get("evidence") or {}).get("url"),
                        "locator": (observation.get("evidence") or {}).get("locator"),
                    }
                    for observation in claim.get("observations") or []
                ],
            }
        )
    coverage = _as_dict(bundle.get("coverage_detail") or result.get("coverage") or {})
    return {
        "goal": bundle.get("goal", result.get("goal", "")),
        "coverage": {
            "planned_pages": coverage.get("planned_pages", 0),
            "fetched_pages": coverage.get("fetched_pages", 0),
            "truncated": bool(coverage.get("truncated")),
            "complete": bool(coverage.get("complete", True)),
            "errors": [
                str(error.get("code"))
                for error in (bundle.get("errors") or result.get("errors") or [])
                if isinstance(error, dict)
            ],
        },
        "records": records,
        "changes": changes,
        "field_observations": observations,
        "citation_count": len(index),
    }


def _kept(proposal, index, limit) -> list[dict]:
    """Keep only claims whose every citation exists in this bundle.

    A claim that cites something this run cannot prove is not a partly trusted
    claim, it is an unsupported one: the model may have derived the whole
    statement from the fabricated part, so the claim is discarded rather than
    trimmed down to its surviving citations.
    """
    if limit <= 0:
        return []
    raw = proposal.get("claims") if isinstance(proposal, dict) else None
    if not isinstance(raw, list):
        raise TypeError("Provider response is not a claim list")
    claims, seen = [], set()
    for item in raw:
        try:
            claim = SynthesisClaim.model_validate(item)
        except ValidationError:
            # One malformed claim is not a signal to trust the others less or
            # more; it is simply not a claim.
            continue
        text = claim.text.strip()
        if not text or text in seen:
            continue
        evidence, pairs, unprovable = [], set(), False
        for citation in claim.citations:
            key = (citation.url, citation.locator)
            if key in pairs:
                continue  # the same proof cited twice is still one proof
            pairs.add(key)
            proof = index.get(key)
            if proof is None:
                unprovable = True
                break
            try:
                evidence.append(
                    models.Evidence.model_validate(proof).model_dump(mode="json")
                )
            except ValidationError:
                # A stored proof that no longer validates proves nothing.
                unprovable = True
                break
        if unprovable or not evidence:
            continue
        seen.add(text)
        claims.append({"kind": claim.kind, "text": text, "evidence": evidence})
        if len(claims) == limit:
            break
    return claims


def _reason(exc: BaseException) -> str:
    """A short token operators can grep; the traceback stays out of the report."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (json.JSONDecodeError, TypeError, ValueError)):
        return "invalid_response"
    return type(exc).__name__.lower() or "error"


async def synthesize(
    *, store, result: dict, provider, limit: int = 12
) -> tuple[list[dict], list[str]]:
    """Model conclusions that cite this run's stored evidence, or nothing at all.

    Returns ``(claims, warnings)`` where each claim validates as ``models.Claim``.
    Failures never propagate: a missing answer is acceptable, a broken report is not.
    """
    if provider is None:
        return [], []
    try:
        result = _as_dict(result)
        identifier = result.get("snapshot_id")
        if not identifier:
            return [], []
        try:
            bundle = store.bundle(identifier) or {}
        except Exception:  # noqa: BLE001 — a pruned snapshot simply has no proof left
            return [], ["synthesis_unavailable_bundle"]
        index = _citation_index(bundle)
        if not index:
            return [], []
        proposal = await provider("synthesis", _payload(bundle, result, index))
        return _kept(proposal, index, limit), []
    except Exception as exc:  # noqa: BLE001 — synthesis is auxiliary to delivery
        return [], [f"synthesis_unavailable_{_reason(exc)}"]
