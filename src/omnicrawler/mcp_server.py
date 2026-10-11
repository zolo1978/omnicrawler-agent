"""MCP stdio adapter. Server authorization is fixed by its launcher."""

from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from omnicrawler.pipeline import CollectionConfig
from omnicrawler.provider import provider_from_env
from omnicrawler.service import TaskService, worker_lock
from omnicrawler.session import SessionStore


def build_server(path: Path, allowed_hosts: set[str]):
    manager = TaskService(path)
    sessions = SessionStore(
        manager, allowed_hosts=allowed_hosts, provider=provider_from_env()
    )

    @asynccontextmanager
    async def lifespan(server):
        with worker_lock(path):
            await manager.start()
            try:
                yield manager
            finally:
                await manager.stop()

    server = FastMCP("OmniCrawler", lifespan=lifespan)

    @server.tool()
    def start_session() -> dict:
        """Start recoverable guidance; first choose once or monitor."""
        return sessions.start()

    @server.tool()
    def answer_questions(session_id: str, answers: dict) -> dict:
        """Answer a partial questionnaire; scope/selector proposals need confirmation."""
        return sessions.answer(session_id, answers)

    @server.tool()
    def get_draft(session_id: str) -> dict:
        """Read draft and revision; unknowns must be resolved before confirmation."""
        return sessions.get(session_id)

    @server.tool()
    async def preview_session(session_id: str) -> dict:
        """Sample one explicitly authorized source using the existing access policy."""
        return await sessions.preview(session_id)

    @server.tool()
    def confirm_session(session_id: str, revision: int) -> dict:
        """Authorize this exact reviewed draft revision; creates task or monitor."""
        return sessions.confirm(session_id, revision, actor="local-mcp-client")

    @server.tool()
    async def task_report(task_id: str, format: str = "md") -> dict:
        """Deliver a completed evidence report, with explicit format warnings."""
        if format not in {"md", "docx", "pdf", "json"}:
            raise ValueError("Unsupported report format")
        return await manager.generate_report(task_id, formats=[format])

    @server.tool()
    def create_task(config: dict, idempotency_key: str | None = None) -> dict:
        """Queue an authorized collection plan and return its task ID."""
        parsed = CollectionConfig.model_validate(config)
        if parsed.local_origins or not set(parsed.allowed_hosts) <= allowed_hosts:
            raise ValueError("Source policy exceeds launcher authorization")
        return {"id": manager.enqueue(parsed, idempotency_key)}

    @server.tool()
    def task_status(task_id: str) -> dict:
        """Read task status, errors and completed result metadata."""
        return manager.get(task_id)

    @server.tool()
    def task_result(task_id: str) -> dict:
        """Read source-traceable structured records for a completed task."""
        task = manager.get(task_id)
        if not task["result"]:
            raise ValueError("Result not available")
        return manager.store.bundle(task["result"]["snapshot_id"])

    @server.tool()
    def cancel_task(task_id: str) -> dict:
        """Cancel a queued or running task."""
        manager.cancel(task_id)
        return manager.get(task_id)

    @server.tool()
    def list_changes() -> list[dict]:
        """Read persisted change events; values are observations, not analysis."""
        return manager.store.traced_events()

    return server
