import pytest

from omnicrawler import core


def test_rule_plan_is_deterministic_and_deduplicates_urls():
    plan = core.make_plan(
        ["https://example.com/p", "https://example.com/p"], ["price"], 10
    )
    assert plan == core.make_plan(["https://example.com/p"], ["price"], 10)
    assert plan.urls == ("https://example.com/p",)
    assert plan.fields == ("price",)
    assert plan.max_pages == 10


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "http://user:secret@example.com", "bad"]
)
def test_plan_rejects_invalid_or_credential_urls(url):
    with pytest.raises(ValueError):
        core.make_plan([url], ["price"], 10)


@pytest.mark.parametrize("budget", [0, -1, True])
def test_plan_rejects_invalid_budget(budget):
    with pytest.raises(ValueError):
        core.make_plan(["https://example.com"], ["price"], budget)


def test_complete_snapshot_detects_add_modify_and_delete():
    old = core.Snapshot(
        "old",
        "catalog",
        {"A": {"price": 79, "currency": "USD"}, "B": {"price": 20}},
        True,
    )
    new = core.Snapshot(
        "new",
        "catalog",
        {"A": {"price": 89, "currency": "USD"}, "C": {"price": 129}},
        True,
    )
    changes = core.detect_changes(old, new)
    assert [(x.kind, x.entity_id) for x in changes] == [
        ("modified", "A"),
        ("removed", "B"),
        ("added", "C"),
    ]
    assert changes[0].before == {"price": 79, "currency": "USD"}
    assert changes[0].after == {"price": 89, "currency": "USD"}
    assert changes == core.detect_changes(old, new)


def test_incomplete_snapshot_cannot_prove_deletion_or_modification():
    old = core.Snapshot("old", "catalog", {"A": {"price": 79}, "B": {}}, True)
    new = core.Snapshot("new", "catalog", {"A": {}}, False)
    assert core.detect_changes(old, new) == ()


def test_changed_coverage_cannot_prove_deletion():
    old = core.Snapshot("old", "page1", {"A": {}}, True)
    new = core.Snapshot("new", "page2", {}, True)
    assert core.detect_changes(old, new) == ()


def test_missing_field_is_unknown_not_change():
    old = core.Snapshot("old", "catalog", {"A": {"price": 79}}, True)
    new = core.Snapshot("new", "catalog", {"A": {}}, True)
    assert core.detect_changes(old, new) == ()


def test_baseline_and_same_snapshot_have_no_changes():
    snapshot = core.Snapshot("one", "catalog", {"A": {}}, True)
    assert core.detect_changes(None, snapshot) == ()
    assert core.detect_changes(snapshot, snapshot) == ()
