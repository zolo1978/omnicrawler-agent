import asyncio
import json

import pytest

from omnicrawler import pipeline, transport
from omnicrawler.policy import AccessDenied
from omnicrawler.storage import SnapshotStore


def fetcher(origin):
    return transport.SafeFetcher({"127.0.0.1"}, local_origins={origin}, delay=0)


def test_real_http_robots_and_redirect_are_enforced(site):
    origin, state = site

    async def run():
        client = fetcher(origin)
        page = await client.fetch(origin + "/products")
        assert page.status == 200 and "data-id" in page.html
        with pytest.raises(AccessDenied):
            await client.fetch(origin + "/private")
        with pytest.raises(AccessDenied):
            await client.fetch(origin + "/redirect")
        with pytest.raises(AccessDenied):
            await client.fetch(origin + "/denied")

    asyncio.run(run())
    assert "/private" not in state["hits"]


def test_default_fetcher_never_contacts_local_site(site):
    origin, state = site
    with pytest.raises(AccessDenied):
        asyncio.run(transport.SafeFetcher({"127.0.0.1"}, delay=0).fetch(origin))
    assert state["hits"] == []


def test_three_runs_save_evidence_and_changes_without_duplicates(site, tmp_path):
    origin, state = site
    store = SnapshotStore(tmp_path / "db.sqlite")
    config = pipeline.CollectionConfig(
        urls=[origin + "/products"],
        allowed_hosts=["127.0.0.1"],
        local_origins=[origin],
        item_selector="article",
        id_attribute="data-id",
        fields={"price": ".price"},
        delay=0,
    )

    async def run():
        first = await pipeline.collect(config, store)
        assert first.status == "succeeded" and first.changes == []
        state["products"] = [("A", "89"), ("B", "20"), ("C", "129")]
        second = await pipeline.collect(config, store)
        assert second.previous_snapshot_id == first.snapshot_id
        assert [(x.kind, x.entity_id) for x in second.changes] == [
            ("modified", "A"),
            ("added", "C"),
        ]
        state["products"] = [("A", "89"), ("C", "129")]
        third = await pipeline.collect(config, store)
        assert [(x.kind, x.entity_id) for x in third.changes] == [("removed", "B")]
        bundle = store.bundle(third.snapshot_id)
        assert bundle["records"][0]["evidence"]["price"]["url"] == origin + "/products"
        store.compare(second.snapshot_id, third.snapshot_id)
        assert len(store.events()) == 3
        assert "89" in json.dumps(bundle)

    asyncio.run(run())


def test_missing_required_field_makes_run_partial(site, tmp_path):
    origin, _state = site
    config = pipeline.CollectionConfig(
        urls=[origin + "/products"],
        allowed_hosts=["127.0.0.1"],
        local_origins=[origin],
        item_selector="article",
        id_attribute="data-id",
        fields={"price": ".missing"},
        delay=0,
    )
    result = asyncio.run(
        pipeline.collect(config, SnapshotStore(tmp_path / "db.sqlite"))
    )
    assert result.status == "partial"
    assert result.changes == []


def test_refusal_status_stops_even_if_response_body_is_broken(site, tmp_path):
    origin, _state = site
    config = pipeline.CollectionConfig(
        urls=[origin + "/deniedbig"],
        allowed_hosts=["127.0.0.1"],
        local_origins=[origin],
        item_selector="article",
        id_attribute="data-id",
        fields={"price": ".price"},
        delay=0,
    )
    result = asyncio.run(
        pipeline.collect(config, SnapshotStore(tmp_path / "db.sqlite"))
    )
    assert result.status == "blocked"


def test_source_deletion_includes_conflicting_claim_evidence(site, tmp_path):
    origin, state = site
    state["by_path"] = {"/a": [("A", "79")], "/b": [("A", "89")]}
    config = pipeline.CollectionConfig(
        urls=[origin + "/a", origin + "/b"],
        allowed_hosts=["127.0.0.1"],
        local_origins=[origin],
        item_selector="article",
        id_attribute="data-id",
        fields={"price": ".price"},
        delay=0,
    )
    store = SnapshotStore(tmp_path / "db.sqlite")
    result = asyncio.run(pipeline.collect(config, store))
    assert result.status == "partial"
    bundle = store.bundle(result.snapshot_id)
    assert bundle["claims"][0]["status"] == "disputed"
    assert store.delete_scope(source_url=origin + "/b") == 1
    with pytest.raises(KeyError):
        store.bundle(result.snapshot_id)
