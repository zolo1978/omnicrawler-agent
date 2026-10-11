import asyncio
import json

from omnicrawler import demo


def test_complete_demo_writes_traceable_three_run_report(tmp_path):
    report = asyncio.run(demo.run_demo(tmp_path))
    assert report["verified"]
    assert [len(run["changes"]) for run in report["runs"]] == [0, 2, 1]
    assert json.loads((tmp_path / "report.json").read_text())["verified"]
    assert (tmp_path / "report.md").exists()
