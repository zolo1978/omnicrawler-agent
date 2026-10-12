import asyncio
import json
import subprocess
import sys
import threading
import time
import types
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import chromium_available, installed
from fastapi.testclient import TestClient

from omnicrawler import api, pipeline, service
from omnicrawler.pipeline import CollectionConfig


@contextmanager
def search_backend(results):
    """A minimal SearXNG-shaped backend so nomination tests stay offline."""
    payload = json.dumps({"results": [{"url": url} for url in results]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def search():
    with search_backend(
        ["http://127.0.0.1:9/product", "https://evil.example/x"]
    ) as url:
        yield url


def configuration():
    return CollectionConfig(
        urls=["https://example.com/products"],
        allowed_hosts=["example.com"],
        item_selector="article",
        id_attribute="data-id",
        fields={"price": ".price"},
    )


def local_configuration(origin):
    return CollectionConfig(
        urls=[origin + "/products"],
        allowed_hosts=["127.0.0.1"],
        local_origins=[origin],
        item_selector="article",
        id_attribute="data-id",
        fields={"price": ".price"},
        delay=0,
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


@pytest.mark.parametrize(
    "engine",
    [
        "html",
        pytest.param(
            "crawl4ai",
            marks=pytest.mark.skipif(
                not installed("crawl4ai"), reason="crawl4ai extra is not installed"
            ),
        ),
        pytest.param(
            "playwright",
            marks=pytest.mark.skipif(
                not chromium_available(), reason="no downloaded Chromium available"
            ),
        ),
    ],
)
def test_service_executes_real_collection_and_exports(site, tmp_path, engine):
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


def test_worker_lock_rejects_a_second_worker_until_release(tmp_path):
    path = tmp_path / "db.sqlite"
    script = (
        "import sys\n"
        "from omnicrawler.service import worker_lock\n"
        "try:\n"
        "    with worker_lock(sys.argv[1]):\n"
        "        print('acquired')\n"
        "except BlockingIOError as exc:\n"
        "    print(f'blocked: {exc}')\n"
        "    raise SystemExit(3)\n"
    )
    with service.worker_lock(path):
        # Same-process reentry must also fail fast: a second worker in one
        # interpreter is exactly the double-recovery the lock exists to stop.
        with pytest.raises(BlockingIOError), service.worker_lock(path):
            pass
        blocked = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert blocked.returncode == 3, blocked.stderr
        assert "already holds" in blocked.stdout
    released = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert released.returncode == 0, released.stderr
    assert "acquired" in released.stdout


def test_run_contract_reaches_task_status_and_result(site, tmp_path):
    origin, state = site
    state["routes"]["/products"] = (500, "<html></html>")
    manager = service.TaskService(tmp_path / "db.sqlite")
    config = local_configuration(origin)
    identifier = manager.enqueue(config)
    asyncio.run(manager.run(identifier))

    task = manager.get(identifier)
    assert task["status"] == "partial"
    # The operator reads code, url and message straight off the task status.
    assert task["errors"][0]["code"] == "http_status"
    assert task["errors"][0]["url"] == origin + "/products"
    assert "500" in task["errors"][0]["message"]
    assert task["warnings"] == []
    assert task["coverage"]["complete"] is False
    assert task["coverage"]["fetched_pages"] == 0
    assert task["engine"] == pipeline.select_engine(config)

    headers = {"X-API-Key": "test-key"}
    with TestClient(
        api.create_app(
            tmp_path / "rest.sqlite",
            "test-key",
            allowed_hosts={"127.0.0.1"},
            local_origins={origin},
        )
    ) as client:
        response = client.post(
            "/tasks", json=config.model_dump(mode="json"), headers=headers
        )
        rest_id = response.json()["id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            body = client.get(f"/tasks/{rest_id}", headers=headers).json()
            if body["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        assert body["status"] == "partial", body
        assert body["errors"][0]["url"] == origin + "/products"
        payload = client.get(f"/tasks/{rest_id}/result", headers=headers).json()
        assert payload["status"] == "partial"
        assert payload["errors"][0]["code"] == "http_status"
        assert "500" in payload["errors"][0]["message"]
        assert payload["coverage"]["complete"] is False
        assert payload["engine"] == pipeline.select_engine(config)
        assert payload["records"] == []


def test_crashed_task_reports_structured_errors_everywhere(tmp_path, monkeypatch):
    async def exploding(config, store):
        raise ValueError("collector exploded")

    monkeypatch.setattr(service, "collect", exploding)
    headers = {"X-API-Key": "test-key"}
    config = configuration().model_dump(mode="json")
    with TestClient(
        api.create_app(
            tmp_path / "db.sqlite", "test-key", allowed_hosts={"example.com"}
        )
    ) as client:
        identifier = client.post("/tasks", json=config, headers=headers).json()["id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            task = client.get(f"/tasks/{identifier}", headers=headers).json()
            if task["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        assert task["status"] == "failed", task
        # The bare class name used to be the whole story; now it is the code of a
        # deviation that also carries the exception message.
        assert task["error"] == "ValueError"
        assert task["errors"] == [
            {
                "code": "ValueError",
                "message": "collector exploded",
                "url": None,
                "retryable": False,
            }
        ]
        assert task["coverage"] is None
        response = client.get(f"/tasks/{identifier}/result", headers=headers)
        assert response.status_code == 409
        assert response.json()["detail"]["errors"][0]["message"] == "collector exploded"


def test_interrupted_task_records_a_structured_deviation(tmp_path):
    manager = service.TaskService(tmp_path / "db.sqlite")
    identifier = manager.enqueue(configuration())
    manager.set_status(identifier, "running")
    manager.recover()
    task = manager.get(identifier)
    assert task["status"] == "failed"
    assert task["error"] == "interrupted"
    assert task["errors"][0]["code"] == "interrupted"
    assert task["errors"][0]["retryable"] is True
    assert "rerun it explicitly" in task["errors"][0]["message"]


def test_synthesized_claims_reach_the_report_with_its_warnings(
    site, tmp_path, monkeypatch
):
    origin, _state = site
    manager = service.TaskService(tmp_path / "db.sqlite")
    identifier = manager.enqueue(local_configuration(origin))
    asyncio.run(manager.run(identifier))
    task = manager.get(identifier)
    assert task["status"] == "succeeded"
    evidence = manager.store.bundle(task["result"]["snapshot_id"])["records"][0][
        "evidence"
    ]["price"]
    claims = [
        {
            "kind": "inference",
            "text": "价格区间为 20 到 79",
            "evidence": [evidence],
        }
    ]
    captured = {}

    async def fake_synthesize(*, store, result, provider, limit=12):
        captured.update(
            {
                "store": store,
                "snapshot_id": result["snapshot_id"],
                "provider": provider,
                "limit": limit,
            }
        )
        return claims, ["synthesis_skipped: no model endpoint configured"]

    module = types.ModuleType("omnicrawler.synthesis")
    module.synthesize = fake_synthesize
    monkeypatch.setitem(sys.modules, "omnicrawler.synthesis", module)

    manifest = asyncio.run(manager.generate_report(identifier))
    assert captured["snapshot_id"] == task["result"]["snapshot_id"]
    assert captured["store"] is manager.store
    assert captured["provider"] is None
    assert captured["limit"] == 12
    assert manifest["warnings"] == ["synthesis_skipped: no model endpoint configured"]
    stored = manager.get(identifier)["result"]["delivery"]
    assert stored["warnings"] == ["synthesis_skipped: no model endpoint configured"]
    report = json.loads(
        (tmp_path / "reports" / identifier / "report.json").read_text("utf-8")
    )
    assert report["inferences"][0]["text"] == "价格区间为 20 到 79"
    assert report["inferences"][0]["kind"] == "inference"


def test_synthesis_failure_still_delivers_the_collection(site, tmp_path, monkeypatch):
    origin, _state = site
    manager = service.TaskService(tmp_path / "db.sqlite")
    identifier = manager.enqueue(local_configuration(origin))
    asyncio.run(manager.run(identifier))

    async def broken(**kwargs):
        raise RuntimeError("provider refused")

    module = types.ModuleType("omnicrawler.synthesis")
    module.synthesize = broken
    monkeypatch.setitem(sys.modules, "omnicrawler.synthesis", module)

    manifest = asyncio.run(manager.generate_report(identifier))
    assert set(manifest["files"]) == {"md", "json"}
    assert manifest["warnings"] == ["synthesis_failed: RuntimeError: provider refused"]
    assert (tmp_path / "reports" / identifier / "report.json").exists()


def test_nominate_endpoint_returns_candidates_and_rejections(
    tmp_path, search, monkeypatch
):
    monkeypatch.setenv("OMNICRAWLER_SEARCH_URL", search)
    headers = {"X-API-Key": "test-key"}
    with TestClient(
        api.create_app(tmp_path / "db.sqlite", "test-key", allowed_hosts={"127.0.0.1"})
    ) as client:
        identifier = client.post("/sessions", headers=headers).json()["id"]
        client.post(
            f"/sessions/{identifier}/answer",
            json={"answers": {"task_type": "once", "goal": "产品价格"}},
            headers=headers,
        )
        response = client.post(f"/sessions/{identifier}/nominate", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["candidates"] == ["http://127.0.0.1:9/product"]
        assert body["rejected"] == [
            {
                "code": "search_candidate_rejected",
                "message": "Candidate is outside the authorized HTTP(S) allowlist",
                "url": "https://evil.example/x",
                "retryable": False,
            }
        ]
        # A nomination is a proposal: nothing is collected until it is accepted.
        assert body["answers"].get("source") is None

        accepted = client.post(
            f"/sessions/{identifier}/answer",
            json={"answers": {"accept_nomination": True}},
            headers=headers,
        )
        assert accepted.json()["answers"]["source"] == ["http://127.0.0.1:9/product"]


def test_nominate_without_a_backend_is_a_clear_conflict(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNICRAWLER_SEARCH_URL", raising=False)
    monkeypatch.delenv("OMNICRAWLER_SEARCH_TOKEN", raising=False)
    headers = {"X-API-Key": "test-key"}
    with TestClient(api.create_app(tmp_path / "db.sqlite", "test-key")) as client:
        identifier = client.post("/sessions", headers=headers).json()["id"]
        response = client.post(f"/sessions/{identifier}/nominate", headers=headers)
        assert response.status_code == 409
        assert "No search backend configured" in response.json()["detail"]
        assert (
            client.post("/sessions/missing/nominate", headers=headers).status_code
            == 404
        )
