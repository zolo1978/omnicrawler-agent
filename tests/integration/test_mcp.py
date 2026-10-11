import asyncio

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from omnicrawler import mcp_server


def test_mcp_exposes_tools_and_enforces_source_policy(tmp_path):
    async def run():
        server = mcp_server.build_server(tmp_path / "db.sqlite", {"example.com"})
        names = {tool.name for tool in await server.list_tools()}
        assert {
            "create_task",
            "task_status",
            "task_result",
            "cancel_task",
            "list_changes",
        } <= names
        with pytest.raises(ToolError):
            await server.call_tool(
                "create_task",
                {
                    "config": {
                        "urls": ["https://evil.com"],
                        "allowed_hosts": ["evil.com"],
                        "item_selector": "article",
                        "id_attribute": "data-id",
                        "fields": {"price": ".price"},
                    }
                },
            )

    asyncio.run(run())
