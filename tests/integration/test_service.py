import asyncio

import pytest
from fastapi.testclient import TestClient

from omnicrawler import api, service
from omnicrawler.pipeline import CollectionConfig


def configuration():
    return CollectionConfig(
        urls=["https://example.com/products"],
        allowed_hosts=["example.com"],
        item_selector="article",
        id_attribute="data-id",
        fields={"price": ".price"},
    )


def test_api_key_and_health_semantics(tmp_path):
    with TestClient(api.create_app(tmp_path / "db.sqlite", "test-key")) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 401
        assert (
            client.get("/health/ready", headers={"X-API-Key": "wrong"}).status_code
            == 401
        )
        assert (
            client.get("/health/ready", headers={"X-API-Key": "test-key"}).status_code
            == 200
        )
        assert client.get("/tasks").status_code == 401


def test_api_never_accepts_client_expansion_of_host_policy(tmp_path):
    with TestClient(
        api.create_app(
            tmp_path / "db.sqlite", "test-key", allowed_hosts={"example.com"}
        )
    ) as client:
        config = configuration().model_dump()
        config["allowed_hosts"] = ["evil.com"]
        response = client.post("/tasks", json=config, headers={"X-API-Key": "test-key"})
        assert response.status_code == 403
        config["allowed_hosts"] = ["example.com"]
        config["local_origins"] = ["http://127.0.0.1:8080"]
        assert (
            client.post(
                "/tasks", json=config, headers={"X-API-Key": "test-key"}
            ).status_code
            == 403
        )


def test_idempotent_enqueue_and_cancellation_are_durable(tmp_path):
    manager = service.TaskService(tmp_path / "db.sqlite")
    first = manager.enqueue(configuration(), "same-request")
    assert manager.enqueue(configuration(), "same-request") == first
    manager.cancel(first)
    assert (
        service.TaskService(tmp_path / "db.sqlite").get(first)["status"] == "cancelled"
    )
    with pytest.raises(ValueError):
        other = configuration().model_copy(update={"item_selector": "div"})
        manager.enqueue(other, "same-request")


def test_startup_recovers_interrupted_tasks_without_unbounded_replay(tmp_path):
    manager = service.TaskService(tmp_path / "db.sqlite")
    task_id = manager.enqueue(configuration())
    manager.set_status(task_id, "running")
    manager.recover()
    assert manager.get(task_id)["status"] == "failed"
    assert manager.get(task_id)["error"] == "interrupted"


def test_schedule_survives_restart_and_can_pause(tmp_path):
    manager = service.TaskService(tmp_path / "db.sqlite")
    identifier = manager.schedule(configuration(), 60)
    manager.pause_schedule(identifier)
    second = service.TaskService(tmp_path / "db.sqlite")
    assert second.schedules()[0]["id"] == identifier
    assert second.schedules()[0]["paused"]


def test_delete_snapshot_removes_evidence_and_changes(tmp_path):
    from omnicrawler.core import Snapshot
    from omnicrawler.storage import SnapshotStore

    store = SnapshotStore(tmp_path / "db.sqlite")
    store.save_bundle(
        Snapshot("a", "catalog", {"A": {"price": 1}}, True),
        {"records": [], "created_at": "2020-01-01T00:00:00+00:00"},
    )
    store.save_bundle(
        Snapshot("b", "catalog", {"A": {"price": 2}}, True),
        {"records": [], "created_at": "2020-01-02T00:00:00+00:00"},
    )
    store.compare("a", "b")
    assert store.cleanup(90) == 2
    assert store.events() == ()
    with pytest.raises(KeyError):
        store.bundle("a")


def test_startup_cleans_expired_data_before_accepting_work(tmp_path):
    from datetime import UTC, datetime

    from omnicrawler.core import Snapshot

    async def run():
        manager = service.TaskService(tmp_path / "db.sqlite")
        for identifier, timestamp in [
            ("expired", "2020-01-01T00:00:00+00:00"),
            ("fresh", datetime.now(UTC).isoformat()),
        ]:
            manager.store.save_bundle(
                Snapshot(identifier, "catalog", {"A": {"price": identifier}}, True),
                {"records": [], "created_at": timestamp},
            )
        task_id = manager.enqueue(configuration())
        manager.set_status(task_id, "succeeded", {"snapshot_id": "expired"})
        manager.alert("expired-alert")
        manager.alert("fresh-alert")
        with manager.store._connection() as conn:
            conn.execute(
                "UPDATE alerts SET updated_at=? WHERE code=?",
                ("2020-01-01T00:00:00+00:00", "expired-alert"),
            )
        await manager.start()
        try:
            with pytest.raises(KeyError):
                manager.store.get("expired")
            assert manager.store.get("fresh").id == "fresh"
            assert manager.get(task_id)["result"] is None
            assert [alert["code"] for alert in manager.metrics()["alerts"]] == [
                "fresh-alert"
            ]
        finally:
            await manager.stop()

    asyncio.run(run())


@pytest.mark.parametrize("engine", ["html", "crawl4ai", "playwright"])
def test_service_executes_real_collection_and_exports(site, tmp_path, engine):
    import time

    origin, _state = site
    config = {
        "urls": [origin + "/products"],
        "allowed_hosts": ["127.0.0.1"],
        "local_origins": [origin],
        "item_selector": "article",
        "id_attribute": "data-id",
        "fields": {"price": ".price"},
        "delay": 0,
        "engine": engine,
    }
    headers = {"X-API-Key": "test-key", "Idempotency-Key": "request"}
    with TestClient(
        api.create_app(
            tmp_path / "db.sqlite",
            "test-key",
            allowed_hosts={"127.0.0.1"},
            local_origins={origin},
        )
    ) as client:
        response = client.post("/tasks", json=config, headers=headers)
        assert response.status_code == 202
        identifier = response.json()["id"]
        assert (
            client.post("/tasks", json=config, headers=headers).json()["id"]
            == identifier
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            task = client.get(f"/tasks/{identifier}", headers=headers).json()
            if task["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        assert task["status"] == "succeeded", task
        bundle = client.get(f"/tasks/{identifier}/result", headers=headers).json()
        assert len(bundle["records"]) == 2
        assert bundle["records"][0]["evidence"]["price"]["url"] == origin + "/products"
        assert (
            client.get(f"/tasks/{identifier}/export.csv", headers=headers).status_code
            == 200
        )
        assert client.get("/metrics", headers=headers).json()["tasks"]["succeeded"] == 1
        assert (
            client.delete(f"/tasks/{identifier}/result", headers=headers).status_code
            == 200
        )
        assert (
            client.get(f"/tasks/{identifier}/result", headers=headers).status_code
            == 409
        )


def test_schedule_trigger_deduplicates_and_pause_prevents_enqueue(tmp_path):
    manager = service.TaskService(tmp_path / "db.sqlite")
    identifier = manager.schedule(configuration(), 60)
    manager.trigger_schedule(identifier)
    manager.trigger_schedule(identifier)
    assert len(manager.list_tasks()) == 1
    manager.pause_schedule(identifier)
    manager.trigger_schedule(identifier)
    assert len(manager.list_tasks()) == 1


def test_deleting_old_snapshot_also_scrubs_new_task_change_values(tmp_path):
    from omnicrawler.core import Snapshot

    manager = service.TaskService(tmp_path / "db.sqlite")
    manager.store.save_bundle(
        Snapshot("old", "scope", {"A": {"price": "79"}}, True),
        {"created_at": "2026-10-11T00:00:00+00:00"},
    )
    manager.store.save_bundle(
        Snapshot("new", "scope", {"A": {"price": "89"}}, True),
        {"created_at": "2026-10-11T00:00:00+00:00"},
    )
    identifier = manager.enqueue(configuration())
    manager.set_status(
        identifier,
        "succeeded",
        {
            "snapshot_id": "new",
            "previous_snapshot_id": "old",
            "changes": [{"before": {"price": "79"}}],
        },
    )
    manager.store.delete("old")
    assert manager.get(identifier)["result"] is None


def test_idempotency_key_reuse_is_safe_under_concurrent_enqueue(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    manager = service.TaskService(tmp_path / "db.sqlite")
    with ThreadPoolExecutor(max_workers=4) as executor:
        identifiers = list(
            executor.map(lambda _: manager.enqueue(configuration(), "same"), range(8))
        )
    assert len(set(identifiers)) == 1


def test_scoped_entity_deletion_invalidates_shared_snapshot(tmp_path):
    from omnicrawler.core import Snapshot

    manager = service.TaskService(tmp_path / "db.sqlite")
    manager.store.save_bundle(
        Snapshot("one", "scope", {"A": {"price": "79"}, "B": {"price": "89"}}, True),
        {
            "created_at": "2026-10-11T00:00:00+00:00",
            "records": [{"entity_id": "A", "fields": {"price": "79"}, "evidence": {}}],
        },
    )
    assert manager.store.delete_scope(entity_id="A") == 1
    with pytest.raises(KeyError):
        manager.store.bundle("one")


def test_shutdown_does_not_start_queued_collections(tmp_path, monkeypatch):
    started = asyncio.Event()
    calls = []

    async def suspended_collection(config, store):
        calls.append(config)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "collect", suspended_collection)

    async def run():
        manager = service.TaskService(tmp_path / "db.sqlite")
        ids = [manager.enqueue(configuration()) for _ in range(4)]
        await manager.start()
        try:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(manager.stop(), 0.3)
            assert len(calls) == 1
            assert [manager.get(identifier)["status"] for identifier in ids] == [
                "cancelled",
                "queued",
                "queued",
                "queued",
            ]
        finally:
            for identifier in ids:
                manager.cancel(identifier)
            if manager.worker and not manager.worker.done():
                manager.worker.cancel()
                await asyncio.sleep(0)
                manager.worker.cancel()
                await asyncio.gather(manager.worker, return_exceptions=True)

    asyncio.run(run())
