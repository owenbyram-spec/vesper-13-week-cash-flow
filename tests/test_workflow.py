"""Actual workflow controls: recurrence economics, version scope and recoverability."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import load_workbook

from vesper_cashflow.imports import import_csvs, synthetic_demo
from vesper_cashflow.models import digest, normalize_inputs
from vesper_cashflow.projection import project, recurrence_dates
from vesper_cashflow.proposals import delay_receipts
from vesper_cashflow.store import Store
from vesper_cashflow.exports import csv_export, excel_export, printable_export


def setup_store(tmp_path):
    store = Store(tmp_path)
    scope = store.create_workspace("Synthetic teaching entity", synthetic=True)
    version = store.save_version(synthetic_demo(), "Base", scope=scope)
    return store, version


def test_biweekly_payroll_full_amount_on_every_occurrence():
    inputs = synthetic_demo()
    result = project(inputs)
    payroll = [row for row in result["occurrences"] if row["recurrence_id"] == "payroll"]
    assert len(payroll) == 7
    assert {row["amount"] for row in payroll} == {"1000.00"}
    assert sum(Decimal(row["amount"]) for row in payroll) == Decimal("7000.00")
    assert len(result["weeks"]) == 13
    assert result["weeks"][0]["start"] == "2026-10-26"
    assert result["weeks"][-1]["end"] == "2027-01-24"
    assert result["ending_cash"] == "35000.00"
    assert result["minimum_cash"] == {"amount": "25000.00", "date": "2026-10-25", "basis": "as_of_opening"}
    assert result["coverage_complete"] is True
    assert all(
        Decimal(row["beginning_cash"]) + Decimal(row["cash_in"]) - Decimal(row["cash_out"])
        == Decimal(row["ending_cash"])
        for row in result["weeks"]
    )


def test_weekly_biweekly_monthly_leap_and_effective_bounds():
    row = {"anchor_date": "2028-01-31", "start_date": "2028-01-31", "end_date": None, "cadence": "monthly"}
    assert recurrence_dates(row, date(2028, 1, 1), date(2028, 4, 30)) == [
        date(2028, 1, 31),
        date(2028, 2, 29),
        date(2028, 3, 31),
        date(2028, 4, 30),
    ]
    row.update(start_date="2028-02-01", end_date="2028-03-30")
    assert recurrence_dates(row, date(2028, 1, 1), date(2028, 4, 30)) == [date(2028, 2, 29)]
    row.update(anchor_date="2028-01-01", start_date="2028-01-03", end_date=None, cadence="weekly")
    assert recurrence_dates(row, date(2028, 1, 1), date(2028, 1, 20)) == [date(2028, 1, 8), date(2028, 1, 15)]
    row["cadence"] = "biweekly"
    assert recurrence_dates(row, date(2028, 1, 1), date(2028, 1, 30)) == [
        date(2028, 1, 15),
        date(2028, 1, 29),
    ]


def test_missing_due_expected_overdue_and_beyond_remain_visible():
    inputs = synthetic_demo()
    inputs["coverage"]["ar"] = "partial"
    inputs["items"][0]["expected_date"] = None
    inputs["items"][0]["timing_basis"] = ""
    inputs["items"][1]["due_date"] = None
    inputs["items"][1]["expected_date"] = "2027-06-01"
    inputs["items"][2]["expected_date"] = "2026-10-24"
    result = project(inputs)
    codes = {row["code"] for row in result["findings"]}
    assert {
        "coverage",
        "undated",
        "missing_due_date",
        "beyond_horizon",
        "past_expected",
        "overdue_source",
    } <= codes
    assert result["coverage_complete"] is False
    assert result["omitted_beyond_cash_in"] == "8000.00"
    # Source amounts stay intact; nothing silently retimes past entries into week one.
    assert result["weeks"][0]["cash_in"] == "0.00"
    assert result["weeks"][1]["cash_out"] == "0.00"
    assert inputs["items"][0]["amount"] == "12000.00"


def test_money_precision_and_currency_refuse_implicit_changes():
    raw = synthetic_demo()
    raw["items"][0]["amount"] = 0.1
    with pytest.raises(ValueError, match="decimal string"):
        normalize_inputs(raw)
    raw = synthetic_demo()
    raw["items"][0]["amount"] = "0.001"
    with pytest.raises(ValueError, match="rounding is refused"):
        normalize_inputs(raw)
    raw = synthetic_demo()
    raw["items"][0]["currency"] = "EUR"
    with pytest.raises(ValueError, match="Mixed-currency"):
        normalize_inputs(raw)
    raw = synthetic_demo()
    raw["items"][0]["expected_date"] = "2026-10-30"
    raw["items"][0]["timing_basis"] = ""
    with pytest.raises(ValueError, match="timing basis"):
        normalize_inputs(raw)


def test_csv_preview_hashes_preserve_duplicates_and_stop_invalid_rows():
    root = Path(__file__).parents[1] / "examples"
    raw = {
        "opening_csv": (root / "opening-cash.csv").read_text(),
        "items_csv": (root / "cash-items.csv").read_text(),
        "recurring_csv": (root / "recurring-assumptions.csv").read_text(),
        "coverage": synthetic_demo()["coverage"],
    }
    imported = import_csvs(raw)
    assert project(imported)["ending_cash"] == "35000.00"
    assert imported["sources"][1]["details"].startswith("4 rows")
    bad = raw.copy()
    bad["items_csv"] = bad["items_csv"].replace("12000.00", "not an amount")
    with pytest.raises(ValueError):
        import_csvs(bad)
    bad = raw.copy()
    bad["items_csv"] += "extra,row\n"
    with pytest.raises(ValueError, match="wrong number"):
        import_csvs(bad)
    duplicate = synthetic_demo()
    duplicate["items"].append(deepcopy(duplicate["items"][0]))
    with pytest.raises(ValueError, match="identities"):
        normalize_inputs(duplicate)
    raw["items_csv"] = raw["items_csv"].replace("ar-102", "ar-101")
    with pytest.raises(ValueError, match="identities"):
        import_csvs(raw)


def test_version_save_resume_scope_and_same_hash_exact_version(tmp_path):
    store, base = setup_store(tmp_path)
    assert Store(tmp_path).get_version()["result"] == base["result"]
    captured = store.active_workspace()
    next_version = store.save_version(
        base["inputs"], "Same inputs, second immutable version", base_hash=base["input_hash"]
    )
    assert next_version["id"] != base["id"]
    with pytest.raises(ValueError, match="version changed"):
        store.save_version(base["inputs"], "stale acquisition", base_hash=base["input_hash"], scope=captured)
    foreign = store.create_workspace("Other entity")
    with pytest.raises(ValueError, match="No forecast"):
        store.get_version(base["id"])
    with pytest.raises(ValueError, match="workspace changed"):
        store.save_version(base["inputs"], "foreign acquisition", scope=captured)
    assert store.active_workspace()["id"] == foreign["id"]


def test_proposal_preview_approval_hash_binding_and_stale(tmp_path):
    store, base = setup_store(tmp_path)
    proposal = store.draft_proposal(
        delay_receipts(base["inputs"], 14), "Delayed collections", base_hash=base["input_hash"]
    )
    assert store.get_version()["id"] == base["id"]
    assert proposal["comparison"]["weekly"][0]["delta"] == "-12000.00"
    assert proposal["preview"]["ending_cash"] == "35000.00"
    assert proposal["preview"]["minimum_cash"]["amount"] == "15500.00"
    with pytest.raises(ValueError, match="integrity"):
        store.resolve_proposal(proposal["id"], "0" * 64, True)
    approved = store.resolve_proposal(proposal["id"], proposal["proposal_hash"], True)["version"]
    assert approved["parent_id"] == base["id"]
    assert store.get_version(base["id"])["inputs"]["items"][0]["expected_date"] == "2026-10-30"
    assert approved["inputs"]["items"][0]["due_date"] == "2026-10-20"
    assert approved["inputs"]["items"][0]["expected_date"] == "2026-11-13"
    stale = store.draft_proposal(
        delay_receipts(approved["inputs"], 7), "Another delay", base_hash=approved["input_hash"]
    )
    store.activate_version(base["id"])
    with pytest.raises(ValueError, match="Stale proposal"):
        store.resolve_proposal(stale["id"], stale["proposal_hash"], True)
    assert store.get_version()["id"] == base["id"]


def test_proposals_reject_unauthorized_source_and_foreign_ids(tmp_path):
    store, base = setup_store(tmp_path)
    for changes in [
        [{"kind": "opening", "amount": "99999.00"}],
        [{"kind": "timing", "id": "foreign", "expected_date": "2026-12-01", "timing_basis": "test"}],
        [
            {
                "kind": "timing",
                "id": "ar-101",
                "expected_date": "2026-12-01",
                "timing_basis": "test",
                "amount": "99999.00",
            }
        ],
    ]:
        with pytest.raises(ValueError):
            store.draft_proposal(changes, "Bad proposal", base_hash=base["input_hash"])
    assert len(store.list_versions()) == 1


def test_backup_restore_verifies_before_atomic_new_workspace(tmp_path):
    store, base = setup_store(tmp_path)
    proposal = store.draft_proposal(
        delay_receipts(base["inputs"], 14), "Scenario", base_hash=base["input_hash"]
    )
    store.resolve_proposal(proposal["id"], proposal["proposal_hash"], True)
    backup = store.backup()
    restored = store.restore(backup)
    assert restored["id"] != backup["workspace"]["id"]
    assert len(store.list_workspaces()) == 2
    assert len(store.list_versions()) == 2
    assert store.get_version()["result"] == backup["versions"][-1]["result"]
    bad = deepcopy(backup)
    bad["versions"][0]["inputs"]["opening_cash"] = "9999999.00"
    bad["backup_hash"] = digest({k: v for k, v in bad.items() if k != "backup_hash"})
    with pytest.raises(ValueError, match="verification failed"):
        store.restore(bad)
    assert len(store.list_workspaces()) == 2
    assert Store(tmp_path).get_version()["id"] == store.get_version()["id"]


def test_selected_version_exports_exact_data_and_neutralizes_injection(tmp_path):
    store, base = setup_store(tmp_path)
    raw = deepcopy(base["inputs"])
    raw["items"][0]["name"] = '=HYPERLINK("unsafe")'
    changed = store.save_version(raw, "Export injection case", base_hash=base["input_hash"])
    current = store.active_workspace()
    exported = csv_export(current, changed).decode("utf-8-sig")
    assert "'=HYPERLINK" in exported
    assert changed["input_hash"] in exported
    assert "35000.00" in exported
    workbook = load_workbook(BytesIO(excel_export(current, changed)))
    assert workbook["Cash items"]["B2"].data_type == "s"
    assert workbook["Cash items"]["B2"].value.startswith("'=")
    assert workbook["Cash items"]["E2"].value == 12000
    assert workbook["Cash items"]["E2"].data_type == "n"
    assert workbook["Forecast"]["G14"].value == 35000
    assert workbook["Forecast"]["G14"].data_type == "n"
    html = printable_export(current, changed)
    assert "&quot;unsafe&quot;" in html and "SYNTHETIC" in html
    assert base["input_hash"] in csv_export(current, store.get_version(base["id"])).decode()
    assert "<script" not in html


def test_database_integrity_and_immutable_snapshot(tmp_path):
    store, version = setup_store(tmp_path)
    with pytest.raises(Exception, match="immutable"):
        with store.connection() as db:
            db.execute("UPDATE versions SET label='changed' WHERE id=?", (version["id"],))
    assert store.get_version()["label"] == "Base"
    with pytest.raises(ValueError, match="exactly"):
        store.delete_workspace("wrong name")
    store.delete_workspace("Synthetic teaching entity")
    assert store.active_workspace() is None
    assert store.list_workspaces() == []
