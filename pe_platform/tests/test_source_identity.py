"""Origin and compatibility boundaries that should fail on an accidental fork."""
import hashlib
import json
from pathlib import Path

from pe_platform.provenance import verify_source


def test_native_kernel_origin_and_cash_modules():
    result = verify_source()
    assert result["source_revision"] == "6667bc5b73173d83400a8d269c23f30942e4f00a"
    assert result["source_files_verified"] > 300
    root = Path(__file__).resolve().parents[1] / "upstream/app"
    for name in ("cashflow/projection.py", "cashflow/versioning.py", "cashflow/export_snapshots.py",
                 "routers/cashflow_forecast.py", "agent/runtime/runtime.py", "agent/runtime/model_loop.py"):
        assert (root / name).is_file()


def test_branding_adaptations_keep_original_identity_and_financial_modules():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "source-manifest.json").read_text())
    adapted = []
    for entry in manifest["files"]:
        original = entry.get("source_sha256", entry["sha256"])
        assert len(original) == 64 and len(entry["blob"]) == 40
        current = hashlib.sha256((root / "upstream" / entry["path"]).read_bytes()).hexdigest()
        assert current == entry["sha256"]
        if original != current:
            assert "branding" in entry["adaptation"]
            adapted.append(entry["path"])
        if entry["path"].startswith("app/cashflow/"):
            assert current == original, "Branding must preserve the deterministic financial modules"
    assert adapted
    assert verify_source()["source_files_adapted"] == len(adapted)


def test_legacy_default_remains_separate():
    from start_platform import default_data_dir
    assert default_data_dir().name == "pe-cash-flow"
    assert "13-week-cash-flow" not in str(default_data_dir())
