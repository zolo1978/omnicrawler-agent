"""Robustness contracts: field chains, deviation classes, pagination, pinning, replay."""

import asyncio
import sqlite3

import pytest

from omnicrawler import pipeline
from omnicrawler.storage import SnapshotStore


def configuration(origin, **overrides):
    base = {
        "goal": "监控价格",
        "urls": [origin + "/list"],
        "allowed_hosts": ["127.0.0.1"],
        "local_origins": [origin],
        "item_selector": "article",
        "id_attribute": "data-id",
        "fields": {"price": ".price"},
        "delay": 0,
    }
    return pipeline.CollectionConfig(**{**base, **overrides})


def test_field_selector_chain_survives_a_renamed_class(site, tmp_path):
    origin, state = site
    store = SnapshotStore(tmp_path / "db.sqlite")
    config = configuration(origin, fields={"price": [".amount", ".price"]})
    state["routes"]["/list"] = (
        200,
        '<article data-id="A"><span class="price">79</span></article>',
    )
    first = asyncio.run(pipeline.collect(config, store))
    assert first.status == "succeeded"

    # The site renames the class; the same configuration must keep working.
    state["routes"]["/list"] = (
        200,
        '<article data-id="A"><span class="amount">89</span></article>',
    )
    second = asyncio.run(pipeline.collect(config, store))

    assert second.status == "succeeded"
    assert second.warnings == []
    assert [(change.kind, change.entity_id) for change in second.changes] == [
        ("modified", "A")
    ]


def test_unresolved_field_warns_and_stays_comparable(site, tmp_path):
    origin, state = site
    store = SnapshotStore(tmp_path / "db.sqlite")
    state["routes"]["/list"] = (
        200,
        '<article data-id="A"><span class="price">79</span></article>',
    )
    config = configuration(origin, fields={"price": ".price", "currency": ".currency"})
    result = asyncio.run(pipeline.collect(config, store))

    assert result.status == "succeeded"
    assert [warning.code for warning in result.warnings] == ["field_selector_no_match"]
    assert result.errors == []
    assert result.coverage.complete is True
    records = store.bundle(result.snapshot_id)["records"]
    assert records[0]["fields"] == {"price": "79"}


def test_vanished_field_never_reports_a_deletion(site, tmp_path):
    origin, state = site
    store = SnapshotStore(tmp_path / "db.sqlite")
    config = configuration(origin)
    state["routes"]["/list"] = (
        200,
        (
            '<article data-id="A"><span class="price">79</span></article>'
            '<article data-id="B"><span class="price">20</span></article>'
        ),
    )
    first = asyncio.run(pipeline.collect(config, store))
    assert first.status == "succeeded"

    state["routes"]["/list"] = (
        200,
        (
            '<article data-id="A"><span class="cost">79</span></article>'
            '<article data-id="B"><span class="cost">20</span></article>'
        ),
    )
    second = asyncio.run(pipeline.collect(config, store))

    assert second.status == "partial"
    assert [error.code for error in second.errors] == [
        "entity_unresolved",
        "no_resolvable_records",
    ]
    assert second.changes == []
    assert second.coverage.complete is False
    assert (
        store.latest(pipeline.coverage_id(config, config.urls)).id == first.snapshot_id
    )


def test_pagination_lands_in_one_comparable_snapshot(site, tmp_path):
    origin, state = site
    store = SnapshotStore(tmp_path / "db.sqlite")
    state["routes"]["/list"] = (
        200,
        (
            '<article data-id="A"><span class="price">79</span></article>'
            '<a class="next" href="/page2">next</a>'
        ),
    )
    state["routes"]["/page2"] = (
        200,
        '<article data-id="B"><span class="price">20</span></article>',
    )
    config = configuration(
        origin, pagination={"next_selector": "a.next", "max_pages": 3}
    )
    result = asyncio.run(pipeline.collect(config, store))

    assert result.status == "succeeded"
    assert result.coverage.fetched_pages == 2
    assert result.coverage.planned_pages == 1
    records = store.bundle(result.snapshot_id)["records"]
    assert sorted(record["entity_id"] for record in records) == ["A", "B"]


def test_page_budget_truncation_blocks_deletion_inference(site, tmp_path):
    origin, state = site
    store = SnapshotStore(tmp_path / "db.sqlite")
    state["routes"]["/list"] = (
        200,
        (
            '<article data-id="A"><span class="price">79</span></article>'
            '<a class="next" href="/page2">next</a>'
        ),
    )
    state["routes"]["/page2"] = (
        200,
        (
            '<article data-id="B"><span class="price">20</span></article>'
            '<a class="next" href="/page3">next</a>'
        ),
    )
    state["routes"]["/page3"] = (
        200,
        '<article data-id="C"><span class="price">129</span></article>',
    )
    config = configuration(
        origin, pagination={"next_selector": "a.next", "max_pages": 3}, max_pages=2
    )
    result = asyncio.run(pipeline.collect(config, store))

    assert result.status == "partial"
    assert [error.code for error in result.errors] == ["page_budget_exceeded"]
    assert result.coverage.truncated is True
    assert result.coverage.complete is False
    assert result.changes == []


def test_unavailable_pinned_engine_refuses_to_switch(site, tmp_path, monkeypatch):
    origin, state = site
    store = SnapshotStore(tmp_path / "db.sqlite")
    config = configuration(origin)
    store.pin_engine(pipeline.coverage_id(config, config.urls), "crawl4ai")
    monkeypatch.setattr(pipeline, "engine_available", lambda engine: False)

    result = asyncio.run(pipeline.collect(config, store))

    assert result.status == "blocked"
    assert [error.code for error in result.errors] == ["engine_unavailable"]
    assert result.engine == "crawl4ai"
    assert state["hits"] == []


def test_engine_is_pinned_once_per_coverage(site, tmp_path, monkeypatch):
    origin, _state = site
    store = SnapshotStore(tmp_path / "db.sqlite")
    config = configuration(origin)
    first = asyncio.run(pipeline.collect(config, store))

    monkeypatch.setattr(pipeline, "select_engine", lambda config: "playwright")
    second = asyncio.run(pipeline.collect(config, store))

    assert second.engine == first.engine
    assert store.engine_for(pipeline.coverage_id(config, config.urls)) == first.engine


def test_raw_documents_are_opt_in_and_pruned_with_their_snapshot(site, tmp_path):
    origin, _state = site
    store = SnapshotStore(tmp_path / "db.sqlite")
    first = asyncio.run(pipeline.collect(configuration(origin), store))
    assert store.raw_for_fetch(first.snapshot_id, origin + "/list") is None

    tracked = configuration(origin, store_raw_html=True)
    second = asyncio.run(pipeline.collect(tracked, store))
    document = store.raw_for_fetch(second.snapshot_id, origin + "/list")
    assert document is not None
    assert 'class="price"' in document["html"]
    assert document["captured_at"].endswith("+00:00")

    store.delete(second.snapshot_id)
    with sqlite3.connect(tmp_path / "db.sqlite") as conn:
        # The earlier bundle references the same content hash, so it is still needed.
        assert conn.execute("SELECT count(*) FROM raw_documents").fetchone()[0] == 1
    store.delete(first.snapshot_id)
    with sqlite3.connect(tmp_path / "db.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM raw_documents").fetchone()[0] == 0


def test_structured_errors_name_the_code_and_url(site, tmp_path):
    origin, state = site
    state["routes"]["/list"] = (500, "<html></html>")
    result = asyncio.run(
        pipeline.collect(configuration(origin), SnapshotStore(tmp_path / "db.sqlite"))
    )

    assert result.status == "partial"
    assert [error.code for error in result.errors] == ["http_status"]
    assert result.errors[0].url == origin + "/list"
    assert "500" in result.errors[0].message


def test_invalid_selector_fails_at_configuration_time():
    with pytest.raises(ValueError):
        pipeline.CollectionConfig(
            urls=["https://example.com/"],
            allowed_hosts=["example.com"],
            item_selector="article[[",
            id_attribute="data-id",
            fields={"price": ".price"},
        )


def test_search_discovery_requires_search_settings():
    with pytest.raises(ValueError):
        pipeline.CollectionConfig(
            discovery="search",
            allowed_hosts=["example.com"],
            item_selector="article",
            id_attribute="data-id",
            fields={"price": ".price"},
        )
    # Goal + allowlist alone must be constructible: seeds come from nomination.
    config = pipeline.CollectionConfig(
        discovery="search",
        search={"endpoint": "http://127.0.0.1:8080"},
        allowed_hosts=["example.com"],
        item_selector="article",
        id_attribute="data-id",
        fields={"price": ".price"},
    )
    assert config.urls == []
    assert config.search.endpoint == "http://127.0.0.1:8080"
    assert pipeline.coverage_id(
        config, ["https://example.com/a"]
    ) != pipeline.coverage_id(config, ["https://example.com/b"])


def test_field_can_read_the_item_elements_own_text(site, tmp_path):
    origin, state = site
    store = SnapshotStore(tmp_path / "db.sqlite")
    # Labels carried on the anchor itself, not a descendant: descendant-only matching
    # silently drops these items from the entity universe.
    state["routes"]["/list"] = (
        200,
        (
            '<ul><li><a class="ref" data-id="A" href="/a">first</a></li>'
            '<li><a class="ref" data-id="B" href="/b">second</a></li></ul>'
        ),
    )
    config = configuration(
        origin, item_selector="a.ref", id_attribute="data-id", fields={"label": "a.ref"}
    )
    result = asyncio.run(pipeline.collect(config, store))

    assert result.status == "succeeded"
    assert result.errors == []
    assert result.warnings == []
    records = store.bundle(result.snapshot_id)["records"]
    assert {record["entity_id"]: record["fields"]["label"] for record in records} == {
        "A": "first",
        "B": "second",
    }


def test_previously_recorded_entity_stops_resolving(site, tmp_path):
    origin, state = site
    store = SnapshotStore(tmp_path / "db.sqlite")
    config = configuration(origin)
    state["routes"]["/list"] = (
        200,
        '<article data-id="A"><span class="price">79</span></article>',
    )
    first = asyncio.run(pipeline.collect(config, store))
    assert first.status == "succeeded"

    # The entity keeps its identity but loses every resolvable field: the snapshot
    # must not silently look like a deletion.
    state["routes"]["/list"] = (200, '<article data-id="A"><span>79</span></article>')
    second = asyncio.run(pipeline.collect(config, store))

    assert second.status == "partial"
    assert "entity_unresolved" in [error.code for error in second.errors]
    assert second.changes == []
    assert (
        store.latest(pipeline.coverage_id(config, config.urls)).id == first.snapshot_id
    )


def test_gbk_page_without_declared_charset_is_reported(site, tmp_path):
    origin, state = site
    body = (
        '<html><body><article data-id="A"><span class="name">苹果</span></article></body></html>'
    ).encode("gbk")
    state["routes"]["/list"] = (200, body, {"Content-Type": "text/html"})
    store = SnapshotStore(tmp_path / "db.sqlite")
    result = asyncio.run(
        pipeline.collect(configuration(origin, fields={"name": ".name"}), store)
    )

    assert "text_decoding_lossy" in [warning.code for warning in result.warnings]
    records = store.bundle(result.snapshot_id)["records"]
    assert "\ufffd" in records[0]["fields"]["name"]


def test_gbk_page_with_meta_charset_decodes_correctly(site, tmp_path):
    origin, state = site
    body = (
        '<html><head><meta charset="gbk"></head><body>'
        '<article data-id="A"><span class="name">苹果</span></article></body></html>'
    ).encode("gbk")
    state["routes"]["/list"] = (200, body, {"Content-Type": "text/html"})
    store = SnapshotStore(tmp_path / "db.sqlite")
    result = asyncio.run(
        pipeline.collect(configuration(origin, fields={"name": ".name"}), store)
    )

    assert result.status == "succeeded"
    assert result.warnings == []
    records = store.bundle(result.snapshot_id)["records"]
    assert records[0]["fields"]["name"] == "苹果"
    origin, state = site
    store = SnapshotStore(tmp_path / "db.sqlite")
    state["routes"]["/list"] = (
        200,
        (
            '<article data-id="A"><span class="price">79</span></article>'
            '<article data-id="B"><span class="price">20</span></article>'
            '<article data-id="C"><span class="price">30</span></article>'
        ),
    )
    config = configuration(
        origin, fields={"price": [".price", ".cost"], "stock": ".stock"}
    )
    result = asyncio.run(pipeline.collect(config, store))

    assert result.status == "succeeded"
    assert len(result.warnings) == 1
    assert "3 item(s)" in result.warnings[0].message
    assert result.coverage.complete is True
