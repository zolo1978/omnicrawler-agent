"""Authenticated single-tenant local REST API."""

import csv
import io
import secrets
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from omnicrawler.pipeline import CollectionConfig, select_engine
from omnicrawler.service import TaskService, worker_lock


class DeleteScope(BaseModel):
    entity_id: str | None = None
    source_url: str | None = None


class ScheduleRequest(BaseModel):
    config: CollectionConfig
    seconds: int = Field(ge=10, strict=True)


def create_app(path: Path, api_key: str, *, allowed_hosts=(), local_origins=()):
    if not api_key:
        raise ValueError("API key is required")
    manager = TaskService(path)
    allowed_hosts, local_origins = set(allowed_hosts), set(local_origins)

    @asynccontextmanager
    async def lifespan(app):
        # OS lock prevents multiple workers from independently recovering tasks.
        with worker_lock(path):
            await manager.start()
            try:
                yield
            finally:
                await manager.stop()

    def authenticate(x_api_key: str | None = Header(default=None)):
        if x_api_key is None or not secrets.compare_digest(x_api_key, api_key):
            raise HTTPException(401, "Invalid API key")

    def authorize(config):
        if (
            not set(config.allowed_hosts) <= allowed_hosts
            or not set(config.local_origins) <= local_origins
        ):
            raise HTTPException(403, "Source policy exceeds server authorization")

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager
    secured = [Depends(authenticate)]

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready", dependencies=secured)
    def ready():
        try:
            manager.metrics()
        except sqlite3.Error:
            raise HTTPException(503, "Not ready") from None
        if not manager.worker or manager.worker.done():
            raise HTTPException(503, "Worker unavailable")
        return {"status": "ready"}

    @app.post("/plan", dependencies=secured)
    def plan(config: CollectionConfig):
        authorize(config)
        return {
            "kind": "rule",
            "config": config.model_dump(),
            "selected_engine": select_engine(config),
        }

    @app.get("/tasks", dependencies=secured)
    def tasks():
        return manager.list_tasks()

    @app.post("/tasks", dependencies=secured, status_code=202)
    def create(
        config: CollectionConfig, idempotency_key: str | None = Header(default=None)
    ):
        authorize(config)
        try:
            return {"id": manager.enqueue(config, idempotency_key)}
        except ValueError:
            raise HTTPException(409, "Idempotency conflict") from None

    @app.get("/tasks/{identifier}", dependencies=secured)
    def task(identifier: str):
        try:
            return manager.get(identifier)
        except KeyError:
            raise HTTPException(404, "Unknown task") from None

    @app.post("/tasks/{identifier}/cancel", dependencies=secured)
    def cancel(identifier: str):
        try:
            manager.cancel(identifier)
            return manager.get(identifier)
        except KeyError:
            raise HTTPException(404, "Unknown task") from None

    @app.get("/tasks/{identifier}/result", dependencies=secured)
    def result(identifier: str):
        value = task(identifier)
        if not value["result"]:
            raise HTTPException(409, "Result unavailable")
        try:
            return manager.store.bundle(value["result"]["snapshot_id"])
        except KeyError:
            raise HTTPException(410, "Result expired or deleted") from None

    @app.get("/tasks/{identifier}/export.csv", dependencies=secured)
    def export(identifier: str):
        bundle = result(identifier)
        rows = [
            {"entity_id": record["entity_id"], **record["fields"]}
            for record in bundle["records"]
        ]
        output = io.StringIO()
        keys = sorted({key for row in rows for key in row})
        writer = csv.DictWriter(output, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: "'" + str(value)
                    if str(value).startswith(("=", "+", "-", "@"))
                    else value
                    for key, value in row.items()
                }
            )
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=records.csv"},
        )

    @app.delete("/tasks/{identifier}/result", dependencies=secured)
    def delete_result(identifier: str):
        value = task(identifier)
        if value["result"]:
            manager.store.delete(value["result"]["snapshot_id"])
        return {"status": "deleted"}

    @app.post("/data/delete", dependencies=secured)
    def delete_scope(scope: DeleteScope):
        try:
            return {
                "deleted_snapshots": manager.store.delete_scope(
                    entity_id=scope.entity_id, source_url=scope.source_url
                )
            }
        except ValueError:
            raise HTTPException(422, "Explicit deletion scope required") from None

    @app.get("/changes", dependencies=secured)
    def changes():
        return manager.store.traced_events()

    @app.get("/metrics", dependencies=secured)
    def metrics():
        return manager.metrics()

    @app.get("/schedules", dependencies=secured)
    def schedules():
        return manager.schedules()

    @app.post("/schedules", dependencies=secured, status_code=201)
    def schedule(request: ScheduleRequest):
        authorize(request.config)
        return {"id": manager.schedule(request.config, request.seconds)}

    @app.post("/schedules/{identifier}/pause", dependencies=secured)
    def pause(identifier: str, paused: bool = True):
        try:
            manager.pause_schedule(identifier, paused)
            return {"paused": paused}
        except KeyError:
            raise HTTPException(404, "Unknown schedule") from None

    return app
