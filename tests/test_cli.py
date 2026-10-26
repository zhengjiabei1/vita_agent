import json
import subprocess
import sys
import pytest

from vita_agent.cli import demo


def test_demo_is_end_to_end_and_explicitly_not_a_benchmark(tmp_path):
    summary = demo(tmp_path)
    assert summary["lunch_status"] == summary["hotel_status"] == "executed"
    assert summary["lunch_drafts"] == 3 and summary["hotel_drafts"] == 2
    assert summary["executed_operations"] == 2
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["benchmark_results"] is None
    assert [p["status"] for p in report["preference_history"]] == ["superseded", "active"]
    assert report["lunch"]["attempts"][0]["violations"][0]["code"] == "budget_exceeded"
    assert report["lunch"]["attempts"][1]["violations"][0]["code"] == "preference_conflict"


def test_module_entrypoint(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "vita_agent", "demo", "--output", str(tmp_path)], text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["benchmark_results"] is None


def test_demo_preserves_existing_reports(tmp_path):
    report = tmp_path / "report.json"
    report.write_text("existing report")
    with pytest.raises(ValueError, match="empty"):
        demo(tmp_path)
    assert report.read_text() == "existing report"
