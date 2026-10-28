#!/usr/bin/env python3
"""Run an exact local forecast with Python alone; browser tools are optional."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a local forecast input snapshot and reports. No installation, server or keys."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--demo", action="store_true", help="Use labelled fictional inputs (the default)")
    source.add_argument("--input", type=Path, help="Canonical input JSON; see docs/DATA-FORMAT.md")
    parser.add_argument("--out", type=Path, help="New output folder; existing paths are never overwritten")
    parser.add_argument("--delay-days", type=int, help="Also draft a collection-delay preview, from 1 to 91 days")
    args = parser.parse_args(argv)

    try:
        if sys.version_info < (3, 12):
            raise ValueError("Install Python 3.12 or newer from https://www.python.org/downloads/.")

        # These modules use only the standard library. Do not import the optional
        # web, provider, credential-store or Excel integrations into this path.
        from vesper_cashflow.exports import csv_export, printable_export
        from vesper_cashflow.imports import synthetic_demo
        from vesper_cashflow.models import canonical, normalize_inputs
        from vesper_cashflow.projection import project
        from vesper_cashflow.proposals import apply_changes, delay_receipts
        from vesper_cashflow.store import Store

        output = args.out or (
            Path("exports")
            / ("cash-flow-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
        )
        output = output.expanduser().resolve()
        if output.exists():
            raise ValueError("Output path already exists. Choose a new --out folder; nothing was overwritten.")
        synthetic = args.input is None
        if synthetic:
            inputs = synthetic_demo()
        else:
            input_path = args.input.expanduser().resolve()
            if input_path.stat().st_size > 7_000_000:
                raise ValueError("Input JSON must be smaller than 7 MB.")
            inputs = normalize_inputs(json.loads(input_path.read_text(encoding="utf-8-sig")))

        # Validate both the baseline and requested preview before creating files.
        project(inputs)
        changes = None
        if args.delay_days is not None:
            changes = delay_receipts(inputs, args.delay_days)
            if not changes:
                raise ValueError("No explicitly dated receipts are available for this preview.")
            project(apply_changes(inputs, changes))

        # mkdir is exclusive, including when another run chooses the same path.
        # All state is confined to this new folder, never the browser app's data.
        output.mkdir(parents=True, exist_ok=False, mode=0o700)
        store = Store(output / "workspace")
        workspace = store.create_workspace(
            "Synthetic local forecast" if synthetic else "Local forecast input snapshot",
            currency=inputs["currency"],
            synthetic=synthetic,
        )
        version = store.save_version(inputs, "Input snapshot (review required)", scope=workspace)
        workspace = store.active_workspace()
        preview = None
        if changes:
            preview = store.draft_proposal(
                changes,
                f"Collection delay preview: {args.delay_days} days (not applied)",
                base_hash=version["input_hash"],
                scope=workspace,
            )
            (output / "scenario-preview.json").write_text(
                canonical({"applied": False, "review_required": True, **preview}) + "\n", encoding="utf-8"
            )

        (output / "forecast.json").write_text(
            canonical({"approved": False, "review_required": True, "workspace": workspace, "version": version})
            + "\n",
            encoding="utf-8",
        )
        (output / "forecast.csv").write_bytes(csv_export(workspace, version))
        report = printable_export(workspace, version).replace(
            "<body>",
            "<body><p class='notice'>INPUT SNAPSHOT — REVIEW REQUIRED. No scenario has been approved.</p>",
            1,
        )
        (output / "forecast.html").write_text(report, encoding="utf-8")
        (output / "backup.json").write_text(canonical(store.backup()) + "\n", encoding="utf-8")
        (output / "REVIEW.txt").write_text(
            "INPUT SNAPSHOT — REVIEW REQUIRED\n"
            "Financial calculations are deterministic. This command does not approve inputs or scenarios.\n"
            "forecast.csv, forecast.html and forecast.json describe the saved baseline.\n"
            "When requested, scenario-preview.json is a pending proposal; it has not changed that baseline.\n"
            "backup.json preserves exact inputs, results, provenance and version hashes.\n"
            "You can restore it through the optional local browser tool for review and approval.\n"
            "No API keys, network services, server or browser are used by this command.\n",
            encoding="utf-8",
        )
        result = version["result"]
        print("SYNTHETIC EXAMPLE" if synthetic else "LOCAL INPUT SNAPSHOT")
        print("Review required; nothing was approved.")
        print(f"Output folder: {output}")
        print(f"Opening cash: {result['opening_cash']} {result['currency']}")
        print(f"Week 13 ending cash: {result['ending_cash']} {result['currency']}")
        print(f"Minimum cash: {result['minimum_cash']['amount']} on {result['minimum_cash']['date']}")
        print(f"Coverage: {'reviewed' if result['coverage_complete'] else 'needs review'}")
        print(f"Coverage/timing findings: {len(result['findings'])}")
        if preview:
            minimum = preview["preview"]["minimum_cash"]
            print(f"Unapplied {args.delay_days}-day preview minimum: {minimum['amount']} on {minimum['date']}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"Forecast failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
