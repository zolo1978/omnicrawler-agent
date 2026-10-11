"""Durable single-worker execution and persistent schedule definitions."""

import asyncio
import json
import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from omnicrawler.pipeline import CollectionConfig, collect
from omnicrawler.storage import SnapshotStore

logger = logging.getLogger("omnicrawler")


@contextmanager
def worker_lock(path):
    import fcntl

    with open(str(path) + ".worker.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class TaskService:
    def __init__(self, path: Path):
        self.store = SnapshotStore(path)
        self.running = {}
        self.scheduler = None
        self.worker = None
        self.stopping = False
        with self.store._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, config TEXT NOT NULL,
                    status TEXT NOT NULL, idem TEXT UNIQUE,
                    result TEXT, error TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY, config TEXT NOT NULL,
                    seconds INTEGER NOT NULL, paused INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    code TEXT PRIMARY KEY, count INTEGER NOT NULL, updated_at TEXT NOT NULL
                );
            """)

    def enqueue(self, config: CollectionConfig, key: str | None = None) -> str:
        payload = json.dumps(config.model_dump(), sort_keys=True)
        identifier = str(uuid4())
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if key:
                row = conn.execute(
                    "SELECT id, config FROM tasks WHERE idem=?", (key,)
                ).fetchone()
                if row:
                    if row[1] != payload:
                        raise ValueError(
                            "Idempotency key reused for different configuration"
                        )
                    return row[0]
            conn.execute(
                "INSERT INTO tasks VALUES (?, ?, 'queued', ?, NULL, NULL, ?)",
                (identifier, payload, key, datetime.now(UTC).isoformat()),
            )
        return identifier

    def get(self, identifier):
        with self.store._connection() as conn:
            row = conn.execute(
                "SELECT id, status, result, error FROM tasks WHERE id=?", (identifier,)
            ).fetchone()
        if not row:
            raise KeyError(identifier)
        return {
            "id": row[0],
            "status": row[1],
            "result": json.loads(row[2]) if row[2] else None,
            "error": row[3],
        }

    def list_tasks(self):
        with self.store._connection() as conn:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM tasks ORDER BY rowid DESC LIMIT 100"
                )
            ]
        return [self.get(identifier) for identifier in ids]

    def set_status(self, identifier, status, result=None, error=None):
        with self.store._connection() as conn:
            conn.execute(
                "UPDATE tasks SET status=?, result=?, error=? WHERE id=?",
                (status, json.dumps(result) if result else None, error, identifier),
            )

    def cancel(self, identifier):
        task = self.get(identifier)
        if task["status"] in {"queued", "running"}:
            self.set_status(identifier, "cancelled")
            task = self.running.get(identifier)
            if task is not None:
                task.get_loop().call_soon_threadsafe(task.cancel)

    def recover(self):
        with self.store._connection() as conn:
            conn.execute(
                "UPDATE tasks SET status='failed', error='interrupted' WHERE status='running'"
            )

    async def run(self, identifier):
        with self.store._connection() as conn:
            changed = conn.execute(
                "UPDATE tasks SET status='running' WHERE id=? AND status='queued'",
                (identifier,),
            ).rowcount
            if not changed:
                return
            config = CollectionConfig.model_validate_json(
                conn.execute(
                    "SELECT config FROM tasks WHERE id=?", (identifier,)
                ).fetchone()[0]
            )
        self.running[identifier] = asyncio.current_task()
        started = datetime.now(UTC)
        try:
            result = await collect(config, self.store)
            self.set_status(identifier, result.status, result.model_dump(mode="json"))
            if result.status != "succeeded":
                self.alert(result.status)
            logger.info(
                json.dumps(
                    {
                        "event": "task_finished",
                        "task_id": identifier,
                        "status": result.status,
                        "duration_seconds": (
                            datetime.now(UTC) - started
                        ).total_seconds(),
                    }
                )
            )
        except asyncio.CancelledError:
            self.set_status(identifier, "cancelled")
            if self.stopping:
                raise
        except Exception as exc:  # noqa: BLE001 — isolates an unknown task failure from the worker
            self.set_status(identifier, "failed", error=type(exc).__name__)
            self.alert("failed")
        finally:
            self.running.pop(identifier, None)

    def alert(self, code):
        with self.store._connection() as conn:
            conn.execute(
                "INSERT INTO alerts VALUES (?, 1, ?) ON CONFLICT(code) DO UPDATE SET count=count+1, updated_at=excluded.updated_at",
                (code, datetime.now(UTC).isoformat()),
            )

    def metrics(self):
        with self.store._connection() as conn:
            statuses = dict(
                conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
            )
            alerts = [
                {"code": row[0], "count": row[1], "updated_at": row[2]}
                for row in conn.execute("SELECT * FROM alerts")
            ]
        return {"tasks": statuses, "active": len(self.running), "alerts": alerts}

    def schedule(self, config, seconds):
        if type(seconds) is not int or seconds < 10:
            raise ValueError("Interval must be at least 10 seconds")
        identifier = str(uuid4())
        with self.store._connection() as conn:
            conn.execute(
                "INSERT INTO schedules VALUES (?, ?, ?, 0)",
                (identifier, config.model_dump_json(), seconds),
            )
        if self.scheduler:
            self.register_schedule(identifier, seconds)
        return identifier

    def schedules(self):
        with self.store._connection() as conn:
            return [
                {"id": row[0], "seconds": row[1], "paused": bool(row[2])}
                for row in conn.execute("SELECT id, seconds, paused FROM schedules")
            ]

    def pause_schedule(self, identifier, paused=True):
        with self.store._connection() as conn:
            if not conn.execute(
                "UPDATE schedules SET paused=? WHERE id=?", (int(paused), identifier)
            ).rowcount:
                raise KeyError(identifier)

    def trigger_schedule(self, identifier):
        with self.store._connection() as conn:
            row = conn.execute(
                "SELECT config, seconds, paused FROM schedules WHERE id=?",
                (identifier,),
            ).fetchone()
            if not row or row[2]:
                return
            active = conn.execute(
                "SELECT 1 FROM tasks WHERE idem LIKE ? AND status IN ('queued','running')",
                (f"schedule:{identifier}:%",),
            ).fetchone()
            if active:
                return
        slot = int(datetime.now(UTC).timestamp()) // row[1]
        self.enqueue(
            CollectionConfig.model_validate_json(row[0]),
            f"schedule:{identifier}:{slot}",
        )

    def register_schedule(self, identifier, seconds):
        self.scheduler.add_job(
            self.trigger_schedule,
            "interval",
            seconds=seconds,
            args=[identifier],
            id=identifier,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )

    async def loop(self):
        while not self.stopping:
            with self.store._connection() as conn:
                row = conn.execute(
                    "SELECT id FROM tasks WHERE status='queued' ORDER BY rowid LIMIT 1"
                ).fetchone()
            if row:
                await self.run(row[0])
            else:
                await asyncio.sleep(0.1)

    async def start(self):
        self.stopping = False
        self.recover()
        # Enforce retention even when every service session lasts under 24 hours.
        await asyncio.to_thread(self.cleanup)
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        for schedule in self.schedules():
            self.register_schedule(schedule["id"], schedule["seconds"])
        self.scheduler.add_job(
            self.cleanup, "interval", hours=24, id="retention", max_instances=1
        )
        self.scheduler.start()
        self.worker = asyncio.create_task(self.loop())

    async def stop(self):
        self.stopping = True
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)

    def cleanup(self):
        from datetime import timedelta

        deleted = self.store.cleanup(90)
        cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        with self.store._connection() as conn:
            conn.execute("DELETE FROM alerts WHERE updated_at < ?", (cutoff,))
        return deleted
