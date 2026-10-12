"""Transactional immutable snapshots and idempotent pair comparison."""

import gzip
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from omnicrawler.core import Change, Snapshot, detect_changes


class SnapshotStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bundles (id TEXT PRIMARY KEY REFERENCES snapshots(id) ON DELETE CASCADE, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    old_id TEXT NOT NULL REFERENCES snapshots(id),
                    new_id TEXT NOT NULL REFERENCES snapshots(id),
                    entity_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(old_id, new_id, entity_id)
                );
                CREATE TABLE IF NOT EXISTS collection_engines (
                    coverage TEXT PRIMARY KEY, engine TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS raw_documents (
                    sha256 TEXT PRIMARY KEY, url TEXT NOT NULL,
                    captured_at TEXT NOT NULL, content BLOB NOT NULL
                );
            """)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA secure_delete=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def save(self, snapshot: Snapshot) -> None:
        payload = json.dumps(asdict(snapshot), sort_keys=True, allow_nan=False)
        with self._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO snapshots VALUES (?, ?)", (snapshot.id, payload)
            )
            existing = conn.execute(
                "SELECT payload FROM snapshots WHERE id=?", (snapshot.id,)
            ).fetchone()[0]
            if existing != payload:
                raise ValueError("Snapshot ID already holds different content")

    def get(self, snapshot_id: str) -> Snapshot:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT payload FROM snapshots WHERE id=?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return Snapshot(**json.loads(row[0]))

    def compare(self, old_id: str, new_id: str) -> tuple[Change, ...]:
        changes = detect_changes(self.get(old_id), self.get(new_id))
        with self._connection() as conn:
            for change in changes:
                conn.execute(
                    "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?)",
                    (
                        old_id,
                        new_id,
                        change.entity_id,
                        json.dumps(asdict(change), sort_keys=True, allow_nan=False),
                    ),
                )
        return changes

    def events(self) -> tuple[Change, ...]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM events ORDER BY old_id, new_id, entity_id"
            ).fetchall()
        return tuple(Change(**json.loads(row[0])) for row in rows)

    def latest(self, coverage: str) -> Snapshot | None:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM snapshots ORDER BY rowid DESC"
            ).fetchall()
        for row in rows:
            snapshot = Snapshot(**json.loads(row[0]))
            if snapshot.coverage == coverage and snapshot.complete:
                return snapshot
        return None

    def engine_for(self, coverage: str) -> str | None:
        """The first engine resolved for a coverage wins for every later run."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT engine FROM collection_engines WHERE coverage=?", (coverage,)
            ).fetchone()
        return row[0] if row else None

    def pin_engine(self, coverage: str, engine: str) -> str:
        with self._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO collection_engines VALUES (?, ?)",
                (coverage, engine),
            )
            row = conn.execute(
                "SELECT engine FROM collection_engines WHERE coverage=?", (coverage,)
            ).fetchone()
        return row[0]

    def save_document(
        self, *, digest: str, url: str, captured_at: str, html: str
    ) -> None:
        """Opt-in raw retention, keyed by content hash so repeats store once."""
        blob = gzip.compress(html.encode("utf-8"), compresslevel=6)
        with self._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO raw_documents VALUES (?, ?, ?, ?)",
                (digest, url, captured_at, blob),
            )

    def raw_for_fetch(self, snapshot_id: str, url: str) -> dict | None:
        """Replay the stored document behind one fetch of a snapshot."""
        digest = None
        for fetch in self.bundle(snapshot_id).get("fetches", []):
            if url in {fetch.get("url"), fetch.get("requested_url")}:
                digest = fetch.get("hash")
                break
        if not digest:
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT url, captured_at, content FROM raw_documents WHERE sha256=?",
                (digest,),
            ).fetchone()
        if row is None:
            return None
        return {
            "url": row[0],
            "captured_at": row[1],
            "html": gzip.decompress(row[2]).decode("utf-8", errors="replace"),
        }

    def _prune_documents(self) -> int:
        """Drop raw documents no surviving bundle references any more."""
        with self._connection() as conn:
            referenced = set()
            for (payload,) in conn.execute("SELECT payload FROM bundles").fetchall():
                for fetch in json.loads(payload).get("fetches", []):
                    if fetch.get("hash"):
                        referenced.add(fetch["hash"])
            orphaned = [
                row[0]
                for row in conn.execute("SELECT sha256 FROM raw_documents").fetchall()
                if row[0] not in referenced
            ]
            for digest in orphaned:
                conn.execute("DELETE FROM raw_documents WHERE sha256=?", (digest,))
        return len(orphaned)

    def save_bundle(self, snapshot: Snapshot, bundle: dict) -> None:
        # Bundle and snapshot are committed together; no partially stored evidence.
        payload = json.dumps(asdict(snapshot), sort_keys=True, allow_nan=False)
        evidence = json.dumps(bundle, sort_keys=True, allow_nan=False)
        with self._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO snapshots VALUES (?, ?)", (snapshot.id, payload)
            )
            if (
                conn.execute(
                    "SELECT payload FROM snapshots WHERE id=?", (snapshot.id,)
                ).fetchone()[0]
                != payload
            ):
                raise ValueError("Snapshot ID already holds different content")
            conn.execute(
                "INSERT OR IGNORE INTO bundles VALUES (?, ?)", (snapshot.id, evidence)
            )
            if (
                conn.execute(
                    "SELECT payload FROM bundles WHERE id=?", (snapshot.id,)
                ).fetchone()[0]
                != evidence
            ):
                raise ValueError("Snapshot evidence cannot be overwritten")

    def bundle(self, snapshot_id: str) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT payload FROM bundles WHERE id=?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return json.loads(row[0])

    def delete(self, snapshot_id: str, *, prune: bool = True) -> None:
        with self._connection() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "report_artifacts" in tables:
                for (identifier,) in conn.execute(
                    "SELECT task_id FROM report_artifacts WHERE snapshot_id=?",
                    (snapshot_id,),
                ).fetchall():
                    self._delete_report_files(identifier)
            conn.execute(
                "DELETE FROM events WHERE old_id=? OR new_id=?",
                (snapshot_id, snapshot_id),
            )
            conn.execute("DELETE FROM snapshots WHERE id=?", (snapshot_id,))
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "tasks" in tables:
                rows = conn.execute(
                    "SELECT id, result FROM tasks WHERE result IS NOT NULL"
                ).fetchall()
                for identifier, payload in rows:
                    result = json.loads(payload)
                    if snapshot_id in {
                        result.get("snapshot_id"),
                        result.get("previous_snapshot_id"),
                    }:
                        conn.execute(
                            "UPDATE tasks SET result=NULL, error='expired_or_deleted' WHERE id=?",
                            (identifier,),
                        )
                        self._delete_report_files(identifier)
        if prune:
            self._prune_documents()

    def _delete_report_files(self, identifier):
        import shutil
        from uuid import UUID

        try:
            UUID(identifier)
        except ValueError:
            return
        directory = self.path.parent / "reports" / identifier
        if directory.exists():
            # Propagate filesystem failure so the surrounding DB transaction
            # rolls back and the caller can retry instead of claiming deletion.
            shutil.rmtree(directory)

    def cleanup(self, retention_days: int = 90) -> int:
        from datetime import UTC, datetime, timedelta

        if retention_days < 0:
            raise ValueError("Retention cannot be negative")
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        with self._connection() as conn:
            rows = conn.execute("SELECT id, payload FROM bundles").fetchall()
        expired = [
            identifier
            for identifier, payload in rows
            if datetime.fromisoformat(json.loads(payload)["created_at"]) < cutoff
        ]
        for identifier in expired:
            self.delete(identifier, prune=False)
        self._prune_documents()
        self._clear_previews(cutoff=cutoff)
        return len(expired)

    def _clear_previews(self, *, entity_id=None, source_url=None, cutoff=None):
        from datetime import datetime

        with self._connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='guidance_sessions'"
            ).fetchone():
                return
            rows = conn.execute(
                "SELECT id, answers, preview, confirmation, created_at FROM guidance_sessions"
            ).fetchall()
            for identifier, answers_raw, preview_raw, confirmation, created_at in rows:
                answers = json.loads(answers_raw)
                preview = json.loads(preview_raw) if preview_raw else {}
                source_match = source_url is not None and (
                    source_url in answers.get("source", [])
                    or source_url == preview.get("source_url")
                    or any(
                        source_url == proof.get("url")
                        for sample in preview.get("samples", [])
                        for proof in sample.get("evidence", {}).values()
                    )
                )
                entity_match = entity_id is not None and any(
                    sample.get("entity_id") == entity_id
                    for sample in preview.get("samples", [])
                )
                expired = (
                    cutoff is not None
                    and preview_raw is not None
                    and datetime.fromisoformat(preview.get("captured_at", created_at))
                    < cutoff
                )
                if source_match or entity_match or expired:
                    conn.execute(
                        "UPDATE guidance_sessions SET preview=NULL, revision=revision+? WHERE id=?",
                        (0 if confirmation else 1, identifier),
                    )

    def traced_events(self) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT old_id, new_id, entity_id, payload FROM events ORDER BY rowid"
            ).fetchall()
        output = []
        for old_id, new_id, entity, payload in rows:
            proof = {}
            for label, identifier in [("before", old_id), ("after", new_id)]:
                try:
                    bundle = self.bundle(identifier)
                    proof[label] = [
                        record
                        for record in bundle.get("records", [])
                        if record["entity_id"] == entity
                    ]
                except KeyError:
                    proof[label] = []
            output.append(
                {
                    **json.loads(payload),
                    "previous_snapshot_id": old_id,
                    "snapshot_id": new_id,
                    "evidence": proof,
                }
            )
        return output

    def delete_scope(
        self, *, entity_id: str | None = None, source_url: str | None = None
    ) -> int:
        if not entity_id and not source_url:
            raise ValueError("An explicit deletion scope is required")
        with self._connection() as conn:
            rows = conn.execute("SELECT id, payload FROM bundles").fetchall()
        matches = []
        for identifier, payload in rows:
            bundle = json.loads(payload)
            records = bundle.get("records", [])
            claims = bundle.get("claims", [])
            entity_match = entity_id is not None and (
                any(record["entity_id"] == entity_id for record in records)
                or any(claim.get("entity_id") == entity_id for claim in claims)
            )
            source_references = {
                evidence.get("url")
                for record in records
                for evidence in record.get("evidence", {}).values()
            }
            source_references.update(
                observation.get("evidence", {}).get("url")
                for claim in claims
                for observation in claim.get("observations", [])
            )
            source_references.update(
                fetch.get("url") for fetch in bundle.get("fetches", [])
            )
            source_references.update(
                fetch.get("requested_url") for fetch in bundle.get("fetches", [])
            )
            source_match = source_url is not None and source_url in source_references

            if entity_match or source_match:
                matches.append(identifier)
        for identifier in matches:
            self.delete(identifier, prune=False)
        self._prune_documents()
        self._clear_previews(entity_id=entity_id, source_url=source_url)
        return len(matches)
