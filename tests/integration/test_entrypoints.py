import asyncio
import json
import os
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def run_cli(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "omnicrawler", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def local_config(origin, **overrides):
    config = {
        "urls": [origin + "/products"],
        "allowed_hosts": ["127.0.0.1"],
        "local_origins": [origin],
        "item_selector": "article",
        "id_attribute": "data-id",
        "fields": {"price": ".price"},
        "delay": 0,
        "engine": "html",
    }
    config.update(overrides)
    return config


def write_config(tmp_path, config, *, encoding="utf-8"):
    path = tmp_path / "config.json"
    # ensure_ascii=False keeps a real non-ASCII goal in the file, which is what a
    # locale-dependent read would corrupt or reject.
    path.write_text(json.dumps(config, ensure_ascii=False), encoding=encoding)
    return path


def test_cli_reads_a_utf8_config_under_a_hostile_locale(site, tmp_path):
    """A non-ASCII goal must survive whatever codec the locale hands the process.

    Reading the config or printing the result with the locale codec mangles the
    goal on a GBK/ASCII host and raises where the bytes are not representable.
    """
    origin, _state = site
    goal = "监控价格与库存：中文目标"
    path = write_config(tmp_path, local_config(origin, goal=goal))
    report_dir = tmp_path / "reports"
    environment = {
        **os.environ,
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "LC_ALL": "C",
        "LANG": "C",
    }
    environment.pop("PYTHONIOENCODING", None)
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnicrawler",
            "collect",
            str(path),
            "--db",
            str(tmp_path / "db.sqlite"),
            "--report-dir",
            str(report_dir),
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        env=environment,
        timeout=60,
    )
    assert process.returncode == 0, process.stderr.decode("utf-8", "replace")
    payload = json.loads(process.stdout.decode("utf-8"))
    assert payload["status"] == "succeeded"
    report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert report["goal"] == goal


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
    assert "replay" in process.stdout


def test_collect_reports_structured_errors_and_exits_two(site, tmp_path):
    origin, state = site
    state["routes"]["/products"] = (500, "<html></html>")
    path = write_config(tmp_path, local_config(origin))
    result = run_cli("collect", str(path), "--db", str(tmp_path / "db.sqlite"))
    assert result.returncode == 2, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "partial"
    # The operator gets the deviation as an object, not as a bare class name.
    assert payload["errors"][0]["code"] == "http_status"
    assert payload["errors"][0]["url"] == origin + "/products"
    assert "500" in payload["errors"][0]["message"]
    assert payload["warnings"] == []
    assert payload["coverage"]["complete"] is False
    assert payload["engine"] == "html"


def test_replay_prints_the_retained_source_of_a_collected_run(site, tmp_path):
    origin, _state = site
    path = write_config(tmp_path, local_config(origin))
    database = tmp_path / "db.sqlite"
    collected = run_cli("collect", str(path), "--db", str(database), "--store-raw-html")
    assert collected.returncode == 0, collected.stderr
    snapshot_id = json.loads(collected.stdout)["snapshot_id"]

    replayed = run_cli(
        "replay", snapshot_id, origin + "/products", "--db", str(database)
    )
    assert replayed.returncode == 0, replayed.stderr
    assert 'data-id="A"' in replayed.stdout
    assert 'class="price"' in replayed.stdout
    assert "captured_at=" in replayed.stderr


def test_replay_explains_that_retention_was_not_enabled(site, tmp_path):
    origin, _state = site
    path = write_config(tmp_path, local_config(origin))
    database = tmp_path / "db.sqlite"
    collected = run_cli("collect", str(path), "--db", str(database))
    assert collected.returncode == 0, collected.stderr
    snapshot_id = json.loads(collected.stdout)["snapshot_id"]

    replayed = run_cli(
        "replay", snapshot_id, origin + "/products", "--db", str(database)
    )
    assert replayed.returncode == 3
    assert replayed.stdout == ""
    assert "无法回放" in replayed.stderr
    assert "--store-raw-html" in replayed.stderr


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
