import asyncio
import sys
from datetime import UTC, datetime

from omnicrawler import models
from omnicrawler import report as reporting
from omnicrawler.core import Snapshot
from omnicrawler.storage import SnapshotStore
from omnicrawler.synthesis import synthesize


def test_report_scopes_pair_and_preserves_evidence(tmp_path):
    store = SnapshotStore(tmp_path / "db.sqlite")
    proof = {
        "url": "https://example.com/item",
        "locator": ".price",
        "captured_at": "2026-10-11T10:00:00+00:00",
        "excerpt": "售价 10",
    }
    for sid, value in [("old", "10"), ("new", "20"), ("unrelated", "30")]:
        store.save_bundle(
            Snapshot(sid, "scope", {"A": {"price": value}}, True),
            {
                "goal": "监控价格",
                "records": [
                    {
                        "entity_id": "A",
                        "fields": {"price": value},
                        "evidence": {"price": proof},
                    }
                ],
                "fetches": [{"url": proof["url"]}],
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
    store.compare("old", "new")
    store.compare("new", "unrelated")
    result = reporting.build_report(
        store,
        {
            "snapshot_id": "new",
            "previous_snapshot_id": "old",
            "status": "succeeded",
            "changes": [
                {
                    "kind": "modified",
                    "entity_id": "A",
                    "before": {"price": "10"},
                    "after": {"price": "20"},
                }
            ],
        },
        task_type="monitor",
        schedule_seconds=60,
    )
    assert len(result.changes) == 1
    assert result.changes[0]["evidence"]["before"][0]["fields"]["price"] == "10"
    assert result.summary["modified"] == 1
    text = reporting.render_markdown(result)
    for part in [
        "监控价格",
        "事实",
        "推断",
        "10",
        "20",
        ".price",
        "2026-10-11T10:00:00+00:00",
        proof["url"],
    ]:
        assert part in text
    assert '"price": "30"' not in text


def test_empty_report_and_untrusted_content():
    report = reporting.Report(
        title="<script>alert(1)</script>",
        generated_at=datetime.now(UTC),
        goal="[click](javascript:alert(1)) x@example.com",
        task_type="once",
        summary={},
        records=[],
        changes=[],
        sources=[],
        schedule_seconds=None,
        facts=[],
        inferences=[],
    )
    text = reporting.render_markdown(report)
    assert "<script>" not in text
    assert "[click](javascript:" not in text
    assert "x@example.com" not in text
    assert "无记录" in text


def test_missing_snapshot_is_explicit(tmp_path):
    store = SnapshotStore(tmp_path / "db.sqlite")
    report = reporting.build_report(store, {"status": "blocked", "snapshot_id": None})
    assert report.status == "blocked"
    assert report.records == []
    assert "blocked" in reporting.render_markdown(report)


def test_partial_report_does_not_claim_comparable_deletions(tmp_path):
    store = SnapshotStore(tmp_path / "db.sqlite")
    store.save_bundle(
        Snapshot("old", "scope", {}, True), {"records": [], "goal": "部分采集"}
    )
    store.save_bundle(Snapshot("new", "scope", {}, False), {"records": []})
    report = reporting.build_report(
        store,
        {
            "snapshot_id": "new",
            "previous_snapshot_id": "old",
            "status": "partial",
            "changes": [],
        },
    )
    assert not any("可比范围比较" in fact for fact in report.facts)
    assert any("不完整" in fact for fact in report.facts)


def test_report_redacts_credentials_nested_values_and_email():
    report = reporting.Report(
        records=[
            {
                "fields": {
                    "password": "secret",
                    "note": "https://user:secret@example.com/path x@example.com",
                }
            }
        ]
    )
    text = reporting.render_markdown(report)
    assert "secret" not in text
    assert "user:" not in text
    assert "x@example.com" not in text


def test_query_credentials_and_bearer_are_redacted():
    report = reporting.Report(
        goal="Authorization: Bearer topsecret",
        sources=["https://example.com/?api_key=secret&token=other&category=public"],
    )
    text = reporting.render_markdown(report)
    for secret in ["topsecret", "secret", "other"]:
        assert secret not in text
    assert "category=public" in text


def test_conflicting_claim_observations_preserve_all_sources(tmp_path):
    store = SnapshotStore(tmp_path / "db.sqlite")
    claims = [
        {
            "entity_id": "A",
            "status": "disputed",
            "observations": [
                {
                    "value": "99",
                    "evidence": {
                        "url": "https://other.com/item",
                        "locator": ".cost",
                        "captured_at": "2026-10-11T10:00:00+00:00",
                    },
                }
            ],
        }
    ]
    store.save_bundle(
        Snapshot("s", "scope", {}, False), {"records": [], "claims": claims}
    )
    report = reporting.build_report(store, {"snapshot_id": "s", "status": "partial"})
    assert report.claims == claims
    assert "https://other.com/item" in report.sources
    assert ".cost" in reporting.render_markdown(report)


def test_embedded_url_and_cookie_headers_are_redacted():
    report = reporting.Report(
        goal="监控 https://example.com/items?token=TOP_SECRET&category=public\nCookie: session=COOKIE_SECRET; theme=dark"
    )
    text = reporting._clean(report.goal)
    assert "TOP_SECRET" not in text
    assert "COOKIE_SECRET" not in text
    assert "category=public" in text


def test_reader_report_uses_plain_labels_and_bounded_details():
    report = reporting.Report(
        summary={"records": 100},
        records=[
            {
                "entity_id": f"card-{i}",
                "fields": {"price": str(i)},
                "evidence": {
                    "price": {"url": "https://example.com/card", "locator": ".price"}
                },
            }
            for i in range(100)
        ],
    )
    text = reporting.render_markdown(report)
    assert '"entity_id"' not in text
    assert '"records":' not in text
    assert "100" in text
    assert "card-0" in text
    assert "card-99" not in text
    assert "JSON" in text
    assert ".price" in text


def test_reader_changes_show_changed_fields_and_keep_machine_evidence():
    change = {
        "kind": "modified",
        "entity_id": "A",
        "before": {"price": "10", "unchanged_detail": "redundant"},
        "after": {"price": "20", "unchanged_detail": "redundant"},
    }
    report = reporting.Report(changes=[change])
    text = reporting.render_markdown(report)
    assert "10" in text and "20" in text
    assert "redundant" not in text
    assert report.changes[0]["before"]["unchanged_detail"] == "redundant"


_PROOF = {
    "url": "https://example.com/item",
    "locator": ".price",
    "captured_at": "2026-10-11T10:00:00+00:00",
    "excerpt": "售价 20",
}


def _store_with_record(tmp_path, **bundle_overrides):
    store = SnapshotStore(tmp_path / "db.sqlite")
    bundle = {
        "goal": "监控价格",
        "records": [
            {
                "entity_id": "A",
                "fields": {"price": "20"},
                "evidence": {"price": dict(_PROOF)},
            }
        ],
        "fetches": [{"url": _PROOF["url"], "requested_url": _PROOF["url"]}],
        "claims": [],
    }
    bundle.update(bundle_overrides)
    store.save_bundle(Snapshot("s", "scope", {"A": {"price": "20"}}, True), bundle)
    return store


def _citation(locator=".price", url="https://example.com/item"):
    return {"url": url, "locator": locator}


def test_synthesize_keeps_cited_claims_and_drops_every_unmatched_citation(tmp_path):
    payloads = []

    async def provider(stage, payload):
        payloads.append((stage, payload))
        return {
            "claims": [
                {
                    "kind": "inference",
                    "text": "价格可能上调",
                    "citations": [_citation()],
                },
                {
                    "kind": "inference",
                    "text": "伪造定位",
                    "citations": [_citation(locator=".fabricated")],
                },
                {
                    "kind": "fact",
                    "text": "伪造来源",
                    "citations": [_citation(url="https://evil.example.com/item")],
                },
                {"kind": "inference", "text": "无据结论", "citations": []},
                {"kind": "opinion", "text": "非法类型", "citations": [_citation()]},
                {"kind": "fact", "text": "价格可能上调", "citations": [_citation()]},
                "not a claim",
            ]
        }

    claims, warnings = asyncio.run(
        synthesize(
            store=_store_with_record(tmp_path),
            result={"snapshot_id": "s"},
            provider=provider,
        )
    )
    assert warnings == []
    assert payloads[0][0] == "synthesis"
    assert [claim["text"] for claim in claims] == ["价格可能上调"]
    assert claims[0]["kind"] == "inference"
    assert models.Claim.model_validate(claims[0]).evidence[0].locator == ".price"
    # The model only ever sees the run's own structured findings.
    payload = payloads[0][1]
    assert payload["goal"] == "监控价格"
    assert payload["records"][0]["evidence"]["price"] == {
        "url": "https://example.com/item",
        "locator": ".price",
    }
    assert "fetches" not in payload and "html" not in payload


def test_synthesize_drops_a_claim_with_one_fabricated_citation(tmp_path):
    async def provider(stage, payload):
        return {
            "claims": [
                {
                    "kind": "inference",
                    "text": "混合引用",
                    "citations": [
                        _citation(),
                        _citation(locator=".fabricated"),
                    ],
                },
                {
                    "kind": "inference",
                    "text": "重复引用同一证据",
                    "citations": [_citation(), _citation()],
                },
            ]
        }

    claims, warnings = asyncio.run(
        synthesize(
            store=_store_with_record(tmp_path),
            result={"snapshot_id": "s"},
            provider=provider,
        )
    )

    assert warnings == []
    # An unprovable citation voids the whole claim: the model may have derived the
    # statement from the part this run cannot prove. A duplicated citation does not.
    assert [claim["text"] for claim in claims] == ["重复引用同一证据"]
    assert len(claims[0]["evidence"]) == 1


def test_synthesize_respects_limit_and_provider_free_runs(tmp_path):
    async def provider(stage, payload):
        return {
            "claims": [
                {
                    "kind": "inference",
                    "text": f"结论 {index}",
                    "citations": [_citation()],
                }
                for index in range(5)
            ]
        }

    claims, warnings = asyncio.run(
        synthesize(
            store=_store_with_record(tmp_path),
            result={"snapshot_id": "s"},
            provider=provider,
            limit=2,
        )
    )
    assert warnings == []
    assert [claim["text"] for claim in claims] == ["结论 0", "结论 1"]
    assert asyncio.run(
        synthesize(
            store=_store_with_record(tmp_path),
            result={"snapshot_id": "s"},
            provider=None,
        )
    ) == ([], [])
    report = reporting.build_report(
        _store_with_record(tmp_path), {"snapshot_id": "s", "status": "succeeded"}
    )
    assert report.inferences == []
    assert "无推断" in reporting.render_markdown(report)


def test_synthesize_failures_never_break_report_delivery(tmp_path):
    async def timing_out(stage, payload):
        raise TimeoutError("provider too slow")

    async def raising(stage, payload):
        raise RuntimeError("boom")

    async def garbage(stage, payload):
        return ["not", "an", "envelope"]

    async def no_claims_key(stage, payload):
        return {"items": []}

    for provider, expected in [
        (timing_out, "synthesis_unavailable_timeout"),
        (raising, "synthesis_unavailable_runtimeerror"),
        (garbage, "synthesis_unavailable_invalid_response"),
        (no_claims_key, "synthesis_unavailable_invalid_response"),
    ]:
        store = _store_with_record(tmp_path)
        claims, warnings = asyncio.run(
            synthesize(store=store, result={"snapshot_id": "s"}, provider=provider)
        )
        assert claims == []
        assert warnings == [expected]
        report = reporting.build_report(
            store, {"snapshot_id": "s", "status": "succeeded"}, inferences=claims
        )
        assert "价格" in reporting.render_markdown(report)
        delivered = asyncio.run(
            reporting.deliver(report, tmp_path / "out", formats=("md", "json"))
        )
        assert delivered["warnings"] == []
        assert set(delivered["files"]) == {"md", "json"}


def test_report_renders_inference_with_its_stored_locator(tmp_path):
    async def provider(stage, payload):
        return {
            "claims": [
                {
                    "kind": "inference",
                    "text": "售价压力上升",
                    "citations": [_citation()],
                }
            ]
        }

    store = _store_with_record(tmp_path)
    claims, _ = asyncio.run(
        synthesize(store=store, result={"snapshot_id": "s"}, provider=provider)
    )
    report = reporting.build_report(
        store, {"snapshot_id": "s", "status": "succeeded"}, inferences=claims
    )
    assert isinstance(report.inferences[0], models.Claim)
    line = next(
        line
        for line in reporting.render_markdown(report).splitlines()
        if "售价压力上升" in line
    )
    assert ".price" in line and "https://example.com/item" in line


def test_report_exposes_coverage_and_structured_error_codes(tmp_path):
    store = _store_with_record(
        tmp_path,
        coverage_detail={
            "planned_pages": 4,
            "fetched_pages": 2,
            "truncated": True,
            "complete": False,
            "warnings": [],
        },
        errors=[
            {
                "code": "http_500",
                "message": "upstream error",
                "url": "https://example.com/item",
                "retryable": True,
            }
        ],
        warnings=[{"code": "field_missing", "message": "no price", "retryable": False}],
    )
    report = reporting.build_report(store, {"snapshot_id": "s", "status": "partial"})
    text = reporting.render_markdown(report)
    assert "计划页面：4；实际抓取：2" in text
    assert "分页被预算截断" in text
    assert "范围完整：否" in text
    unescaped = text.replace("\\", "")
    assert "http_500" in unescaped and "field_missing" in unescaped
    assert "可重试" in text
    assert "采集范围与运行告警" in text


def test_windows_cjk_font_roots_are_searched():
    if sys.platform != "win32":
        return
    family = reporting.cjk_font_family()
    assert family in {"Microsoft YaHei", "SimSun", "SimHei"}


def test_synthesize_drops_citations_whose_stored_proof_is_incomplete(tmp_path):
    async def provider(stage, payload):
        return {
            "claims": [
                {"kind": "inference", "text": "结论", "citations": [_citation()]}
            ]
        }

    store = SnapshotStore(tmp_path / "db.sqlite")
    store.save_bundle(
        Snapshot("s", "scope", {"A": {"price": "1"}}, True),
        {
            "goal": "监控价格",
            "records": [
                {
                    "entity_id": "A",
                    "fields": {"price": "1"},
                    # No excerpt/captured_at: the stored proof proves nothing.
                    "evidence": {
                        "price": {
                            "url": "https://example.com/item",
                            "locator": ".price",
                        }
                    },
                }
            ],
        },
    )
    claims, warnings = asyncio.run(
        synthesize(store=store, result={"snapshot_id": "s"}, provider=provider)
    )
    assert claims == []
    assert warnings == []
