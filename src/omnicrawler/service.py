"""Durable single-worker execution and persistent schedule definitions."""

import asyncio
import json
import logging
from contextlib import contextmanager, nullcontext
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
        self.report_root = self.store.path.parent / "reports"
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
                CREATE TABLE IF NOT EXISTS report_artifacts (
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
                    PRIMARY KEY(task_id, snapshot_id)
                );
            """)

    def enqueue(
        self, config: CollectionConfig, key: str | None = None, *, connection=None
    ) -> str:
        payload = json.dumps(config.model_dump(), sort_keys=True)
        identifier = str(uuid4())
        with (
            self.store._connection()
            if connection is None
            else nullcontext(connection) as conn
        ):
            # Reserve the writer before lookup so concurrent idempotent enqueues
            # cannot both observe an absent key; the context commits or rolls back.
            if connection is None:
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
            self.set_status(identifier, "running", result.model_dump(mode="json"))
            if self.delivery_contract(identifier):
                try:
                    await self.generate_report(identifier)
                except KeyError:
                    pass  # A concurrent authorized deletion invalidates delivery.
                except Exception as exc:  # noqa: BLE001 — delivery failure does not reverse collection
                    with self.store._connection() as conn:
                        row = conn.execute(
                            "SELECT result FROM tasks WHERE id=?", (identifier,)
                        ).fetchone()
                        if row and row[0]:
                            payload = json.loads(row[0])
                            payload["delivery"] = {
                                "files": {},
                                "warnings": [
                                    f"report_delivery_failed: {type(exc).__name__}"
                                ],
                            }
                            conn.execute(
                                "UPDATE tasks SET result=? WHERE id=?",
                                (json.dumps(payload), identifier),
                            )
            current = self.get(identifier)
            self.set_status(
                identifier, result.status, current["result"], current["error"]
            )
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

    def delivery_contract(self, identifier):
        with self.store._connection() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "task_deliveries" not in tables:
                return None
            row = conn.execute(
                "SELECT task_type, seconds, formats FROM task_deliveries WHERE task_id=?",
                (identifier,),
            ).fetchone()
        return (
            {
                "task_type": row[0],
                "schedule_seconds": row[1],
                "formats": json.loads(row[2]),
            }
            if row
            else None
        )

    async def generate_report(self, identifier, *, formats=None):
        import shutil
        from tempfile import TemporaryDirectory

        from omnicrawler.report import build_report, deliver

        task = self.get(identifier)
        if not task["result"]:
            raise KeyError("Result unavailable or expired")
        result = task["result"]
        contract = self.delivery_contract(identifier) or {
            "task_type": "once",
            "schedule_seconds": None,
            "formats": ["md", "json"],
        }
        selected = list(dict.fromkeys(contract["formats"] + list(formats or [])))
        report = build_report(
            self.store,
            result,
            task_type=contract["task_type"],
            schedule_seconds=contract["schedule_seconds"],
        )
        self.report_root.mkdir(parents=True, exist_ok=True)
        # Render outside the transaction, then validate dependencies and install
        # under the same writer lock as deletion. No stale report is resurrected.
        with TemporaryDirectory(prefix=".pending-", dir=self.report_root) as temporary:
            manifest = await deliver(report, Path(temporary), selected)
            with self.store._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT result FROM tasks WHERE id=?", (identifier,)
                ).fetchone()
                current = json.loads(row[0]) if row and row[0] else None
                dependencies = {
                    result["snapshot_id"],
                    result.get("previous_snapshot_id"),
                } - {None}
                if (
                    current is None
                    or current.get("snapshot_id") != result["snapshot_id"]
                    or any(
                        not conn.execute(
                            "SELECT 1 FROM snapshots WHERE id=?", (snapshot,)
                        ).fetchone()
                        for snapshot in dependencies
                    )
                ):
                    raise KeyError("Report dependencies were deleted")
                output = self.report_root / identifier
                shutil.rmtree(output, ignore_errors=True)
                output.mkdir()
                for format, path in manifest["files"].items():
                    destination = output / f"report.{format}"
                    Path(path).replace(destination)
                    manifest["files"][format] = str(destination)
                conn.execute(
                    "DELETE FROM report_artifacts WHERE task_id=?", (identifier,)
                )
                conn.executemany(
                    "INSERT INTO report_artifacts VALUES (?, ?)",
                    [(identifier, snapshot) for snapshot in dependencies],
                )
                current["delivery"] = manifest
                conn.execute(
                    "UPDATE tasks SET result=? WHERE id=?",
                    (json.dumps(current), identifier),
                )
        return manifest

    def schedule(self, config, seconds, *, connection=None):
        if type(seconds) is not int or seconds < 10:
            raise ValueError("Interval must be at least 10 seconds")
        identifier = str(uuid4())
        with (
            self.store._connection()
            if connection is None
            else nullcontext(connection) as conn
        ):
            conn.execute(
                "INSERT INTO schedules VALUES (?, ?, ?, 0)",
                (identifier, config.model_dump_json(), seconds),
            )
        if self.scheduler and connection is None:
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
            conn.execute("BEGIN IMMEDIATE")
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
            task_id = self.enqueue(
                CollectionConfig.model_validate_json(row[0]),
                f"schedule:{identifier}:{slot}",
                connection=conn,
            )
            tables = {
                entry[0]
                for entry in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "schedule_deliveries" in tables:
                delivery = conn.execute(
                    "SELECT formats FROM schedule_deliveries WHERE schedule_id=?",
                    (identifier,),
                ).fetchone()
                if delivery:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_deliveries VALUES (?, 'monitor', ?, ?)",
                        (task_id, row[1], delivery[0]),
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
