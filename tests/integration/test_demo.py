import asyncio
import json

from omnicrawler import demo


def test_complete_demo_writes_traceable_three_run_report(tmp_path):
    report = asyncio.run(demo.run_demo(tmp_path))
    assert report["verified"]
    assert [len(run["changes"]) for run in report["runs"]] == [0, 2, 1]
    assert json.loads((tmp_path / "report.json").read_text())["verified"]
    assert (tmp_path / "report.md").exists()


def test_demo_uses_shared_report_model_for_each_snapshot(tmp_path):
    asyncio.run(demo.run_demo(tmp_path))
    canonical = json.loads((tmp_path / "reports" / "run-2" / "report.json").read_text())
    assert canonical["task_type"] == "monitor"
    assert canonical["summary"]["modified"] == 1
    assert canonical["summary"]["added"] == 1
    assert canonical["changes"][0]["evidence"]
    assert "事实" in (tmp_path / "report.md").read_text()
