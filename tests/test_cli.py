"""Run the source CLI with site-packages disabled and only isolated output paths."""

import csv
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from vesper_cashflow.models import digest


ROOT = Path(__file__).parents[1]
CLI = ROOT / "forecast.py"


def run_cli(tmp_path, *arguments):
    return subprocess.run(
        [sys.executable, "-S", str(CLI), *map(str, arguments)],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )


def read_json(path):
    return json.loads(path.read_text())


def local_inputs():
    return {
        "as_of": "2026-10-25", "currency": "USD", "opening_cash": "1000.01",
        "opening_source": "Local fixture opening evidence", "coverage": {"opening_cash": "complete"},
        "items": [
            {"id": "receipt", "name": "Fixture receipt", "direction": "in", "amount": "0.10",
             "due_date": "2026-10-26", "expected_date": "2026-10-26", "timing_basis": "Explicit fixture date",
             "source": "Local fixture"},
            {"id": "payment", "name": "Fixture payment", "direction": "out", "amount": "0.02",
             "due_date": "2026-10-27", "expected_date": "2026-10-27", "timing_basis": "Explicit fixture date",
             "source": "Local fixture"},
        ],
        "recurrences": [],
        "sources": [{"name": "Local fixture evidence", "sha256": digest({"fixture": "opening and items"})}],
    }


def test_demo_reports_and_unapplied_delay_preserve_immutable_baseline(tmp_path):
    output = tmp_path / "new forecast"
    completed = run_cli(tmp_path, "--demo", "--delay-days", 14, "--out", output)
    assert completed.returncode == 0, completed.stderr
    assert "SYNTHETIC EXAMPLE" in completed.stdout
    assert "nothing was approved" in completed.stdout
    snapshot = read_json(output / "forecast.json")
    version = snapshot["version"]
    assert snapshot["approved"] is False and snapshot["review_required"] is True
    assert version["sequence"] == 1
    assert version["result"]["ending_cash"] == "35000.00"
    assert version["result"]["minimum_cash"]["amount"] == "25000.00"
    assert len(version["result"]["weeks"]) == 13
    payroll = [row for row in version["result"]["occurrences"] if row["recurrence_id"] == "payroll"]
    assert len(payroll) == 7 and all(row["amount"] == "1000.00" for row in payroll)
    preview = read_json(output / "scenario-preview.json")
    assert preview["applied"] is False and preview["status"] == "pending"
    assert preview["base_id"] == version["id"] and preview["base_hash"] == version["input_hash"]
    assert preview["preview"]["minimum_cash"]["amount"] == "15500.00"
    assert preview["preview"]["ending_cash"] == "35000.00"
    assert preview["comparison"]["minimum_cash_delta"] == "-9500.00"
    backup = read_json(output / "backup.json")
    assert backup["backup_hash"] == digest({key: value for key, value in backup.items() if key != "backup_hash"})
    assert backup["versions"] == [version]
    assert len(backup["proposals"]) == 1 and backup["proposals"][0]["status"] == "pending"
    assert not any(row["event"] == "proposal_approved" for row in backup["events"])
    rows = list(csv.reader(StringIO((output / "forecast.csv").read_text(encoding="utf-8-sig"))))
    index = rows.index(["Forecast"])
    for row, week in zip(rows[index + 2:index + 15], version["result"]["weeks"]):
        assert row == [str(week[key]) for key in rows[index + 1]]
        assert Decimal(row[3]) + Decimal(row[4]) - Decimal(row[5]) == Decimal(row[6])
    report = (output / "forecast.html").read_text()
    assert "INPUT SNAPSHOT — REVIEW REQUIRED" in report and "No scenario has been approved" in report
    assert version["id"] in report and version["input_hash"] in report
    with sqlite3.connect(output / "workspace/forecast.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            database.execute("UPDATE versions SET label='Changed'")


def test_own_json_preserves_exact_cents_and_missing_coverage(tmp_path):
    source = tmp_path / "my-inputs.json"
    source.write_text(json.dumps(local_inputs()))
    output = tmp_path / "own-output"
    completed = run_cli(tmp_path, "--input", source, "--out", output)
    assert completed.returncode == 0, completed.stderr
    snapshot = read_json(output / "forecast.json")
    assert snapshot["workspace"]["synthetic"] == 0
    assert snapshot["version"]["result"]["ending_cash"] == "1000.09"
    assert snapshot["version"]["result"]["coverage_complete"] is False
    assert snapshot["version"]["inputs"]["sources"][0]["sha256"] == local_inputs()["sources"][0]["sha256"]
    assert "needs review" in completed.stdout
    assert not (output / "scenario-preview.json").exists()


def test_existing_output_is_never_changed(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep this work")
    completed = run_cli(tmp_path, "--out", output)
    assert completed.returncode == 1 and "already exists" in completed.stderr
    assert list(output.iterdir()) == [marker] and marker.read_text() == "keep this work"


@pytest.mark.parametrize("case", ["malformed", "numeric-money", "invalid-delay", "no-dated-receipts"])
def test_invalid_requests_create_no_output(tmp_path, case):
    source = tmp_path / "bad.json"
    inputs = local_inputs()
    arguments = ["--input", source]
    if case == "malformed":
        source.write_text("{")
    else:
        if case == "numeric-money":
            inputs["opening_cash"] = 1000.01
        if case == "no-dated-receipts":
            inputs["items"][0]["expected_date"] = None
        source.write_text(json.dumps(inputs))
    if case in {"invalid-delay", "no-dated-receipts"}:
        arguments.extend(["--delay-days", 0 if case == "invalid-delay" else 14])
    output = tmp_path / "must-not-exist"
    completed = run_cli(tmp_path, *arguments, "--out", output)
    assert completed.returncode == 1 and "Forecast failed:" in completed.stderr
    assert not output.exists() and "Traceback" not in completed.stderr


def test_no_arguments_use_separate_default_demo_folders(tmp_path):
    for _ in range(2):
        completed = run_cli(tmp_path)
        assert completed.returncode == 0, completed.stderr
    folders = list((tmp_path / "exports").iterdir())
    assert len(folders) == 2 and all((folder / "backup.json").exists() for folder in folders)


def test_cli_uses_no_web_provider_or_network_with_site_packages_disabled(tmp_path):
    script = r"""
import runpy, sys
def audit(event, arguments):
    if event.startswith('socket.') or event == 'subprocess.Popen':
        raise AssertionError('Unexpected network or process action: ' + event)
sys.addaudithook(audit)
sys.argv = [sys.argv[1], '--demo', '--out', sys.argv[2]]
try:
    runpy.run_path(sys.argv[0], run_name='__main__')
except SystemExit as exited:
    assert exited.code == 0
forbidden = {'fastapi', 'uvicorn', 'httpx', 'keyring', 'openpyxl', 'webbrowser'}
assert not forbidden.intersection(sys.modules), forbidden.intersection(sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-S", "-c", script, str(CLI), str(tmp_path / "stdlib-only")],
        cwd=ROOT, capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
