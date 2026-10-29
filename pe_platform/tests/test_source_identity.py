"""Origin and compatibility boundaries that should fail on an accidental fork."""
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


def test_legacy_default_remains_separate():
    from start_platform import default_data_dir
    assert default_data_dir().name == "pe-cash-flow"
    assert "13-week-cash-flow" not in str(default_data_dir())
