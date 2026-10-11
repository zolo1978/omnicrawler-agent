from datetime import UTC, datetime

from omnicrawler import report as reporting
from omnicrawler.core import Snapshot
from omnicrawler.storage import SnapshotStore


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
