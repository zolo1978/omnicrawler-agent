import asyncio
import os
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_module_cli_is_executable():
    process = subprocess.run(
        [sys.executable, "-m", "omnicrawler", "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 0, process.stderr
    assert "collect" in process.stdout and "demo" in process.stdout


def test_real_mcp_stdio_handshake_and_policy(tmp_path):
    async def run():
        args = [
            "-m",
            "omnicrawler",
            "mcp",
            "--db",
            str(tmp_path / "db.sqlite"),
            "--allow-host",
            "example.com",
        ]
        async with (
            stdio_client(
                StdioServerParameters(
                    command=sys.executable, args=args, env=dict(os.environ)
                )
            ) as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert "task_result" in names
            response = await session.call_tool(
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
            assert response.isError

    asyncio.run(run())
