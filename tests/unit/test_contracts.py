from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omnicrawler import models, policy, storage
from omnicrawler.core import Snapshot


def test_record_requires_evidence_for_each_field():
    with pytest.raises(ValidationError):
        models.Record(entity_id="A", fields={"price": 79}, evidence={})
    record = models.Record(
        entity_id="A",
        fields={"price": 79},
        evidence={
            "price": models.Evidence(
                url="https://example.com/p",
                captured_at=datetime.now(UTC),
                locator="#price",
                excerpt="$79",
            )
        },
    )
    assert record.evidence["price"].locator == "#price"


def test_evidence_rejects_naive_time_and_credentials():
    for url, timestamp in [
        ("https://user:password@example.com", datetime.now(UTC)),
        ("https://example.com", datetime(2026, 1, 1)),
    ]:
        with pytest.raises(ValidationError):
            models.Evidence(url=url, captured_at=timestamp, locator="p", excerpt="text")


def test_inferred_claim_requires_label_and_evidence():
    with pytest.raises(ValidationError):
        models.Claim(kind="inference", text="new strategy", evidence=[])


def test_task_cannot_jump_from_queued_to_success():
    task = models.Task(id="t")
    with pytest.raises(ValueError):
        task.transition("succeeded")
    running = task.transition("running")
    assert task.status == "queued"
    assert running.transition("partial").status == "partial"


def test_policy_checks_all_dns_addresses_and_exact_allowlist():
    guard = policy.AccessPolicy({"example.com"})
    assert (
        guard.validate_url("https://example.com/p", ["93.184.216.34"]) == "example.com"
    )
    for url, addresses in [
        ("https://evil.example.com", ["93.184.216.34"]),
        ("https://example.com", ["93.184.216.34", "127.0.0.1"]),
        ("https://example.com", []),
        ("https://example.com", ["::ffff:127.0.0.1"]),
        ("https://example.com:22", ["93.184.216.34"]),
    ]:
        with pytest.raises(policy.AccessDenied):
            guard.validate_url(url, addresses)


def test_robots_denial_and_unavailable_are_fail_closed():
    guard = policy.AccessPolicy({"example.com"})
    assert guard.robots_allowed(
        "https://example.com/public", "User-agent: *\nDisallow: /private"
    )
    assert not guard.robots_allowed(
        "https://example.com/private", "User-agent: *\nDisallow: /private"
    )
    assert not guard.robots_allowed("https://example.com/public", None)


def test_store_roundtrip_and_immutable_snapshot(tmp_path):
    store = storage.SnapshotStore(tmp_path / "test.sqlite")
    snap = Snapshot("one", "catalog", {"A": {"price": 79}}, True)
    store.save(snap)
    store.save(snap)
    assert store.get("one") == snap
    with pytest.raises(ValueError):
        store.save(Snapshot("one", "catalog", {}, True))
    assert store.get("one") == snap


def test_events_are_persisted_once_and_failed_snapshot_cannot_delete(tmp_path):
    store = storage.SnapshotStore(tmp_path / "test.sqlite")
    old = Snapshot("one", "catalog", {"A": {"price": 79}, "B": {}}, True)
    new = Snapshot("two", "catalog", {"A": {"price": 89}}, True)
    store.save(old)
    store.save(new)
    first = store.compare("one", "two")
    assert len(first) == 2
    assert store.compare("one", "two") == first
    assert len(store.events()) == 2
    store.save(Snapshot("failed", "catalog", {}, False))
    assert store.compare("two", "failed") == ()
    assert len(store.events()) == 2


def test_store_is_durable_across_instances(tmp_path):
    path = tmp_path / "test.sqlite"
    storage.SnapshotStore(path).save(Snapshot("one", "catalog", {}, True))
    assert storage.SnapshotStore(path).get("one").complete


@pytest.mark.parametrize(
    "url", ["https://example.com/a b", "https://example.com:99999/p"]
)
def test_evidence_rejects_malformed_url(url):
    with pytest.raises(ValidationError):
        models.Evidence(
            url=url, captured_at=datetime.now(UTC), locator="p", excerpt="text"
        )


def test_literal_ip_cannot_be_disguised_by_supplied_dns_answers():
    guard = policy.AccessPolicy({"127.0.0.1"})
    with pytest.raises(policy.AccessDenied):
        guard.validate_url("http://127.0.0.1", ["93.184.216.34"])
