# Set up a forecast from your accounting records

The default workflow is a local Python script. Accounting connections are optional and separate from the assistant. **You do not need an OpenAI key to import accounting records or calculate a forecast.**

## Choose the path that exists today

| Source | Local CLI | Optional local browser tool |
| --- | --- | --- |
| Fictional demonstration | `--demo`; no packages or keys | Synthetic workspace; no keys |
| Your own files | Canonical input JSON | Three strict CSV templates, or a versioned backup |
| QuickBooks Online API | No direct acquisition command | Implemented connector; your Intuit app and authorisation required |
| Xero API | Not implemented | Not implemented; use reviewed file inputs |

The [QuickBooks guide](QUICKBOOKS-SETUP.md) gives the full connection sequence. The [Xero guide](XERO-SETUP.md) explains the usable export/import path and the separate API implementation still required. These are single-device workflows; there is no Vesper account, hosted connector or accounting writeback.

## 1. Install and run the local script

1. Download [the public ZIP](https://github.com/owenbyram-spec/vesper-13-week-cash-flow/archive/refs/heads/main.zip) and extract it. Keep the extracted source in a folder you control.
2. Install Python **3.12 or newer** from [python.org](https://www.python.org/downloads/). Python itself is required; third-party packages are unnecessary for the CLI.
3. Open Terminal in the extracted folder. On macOS, type `cd `, drag the extracted folder into Terminal, and press Return. On Windows, open a terminal in that folder.
4. Run the demonstration:

```sh
python3.12 forecast.py --demo --out ./exports/first-forecast
```

On Windows use `py -3.12` in place of `python3.12`. Windows execution remains unverified. If another Python 3.12+ installation uses the command `python3` or `python`, use that command after checking its `--version`.

Open `exports/first-forecast/forecast.html` or `forecast.csv`. The fictional baseline ends at **$35,000**. For a separate, unapplied fourteen-day collection-delay preview:

```sh
python3.12 forecast.py --demo --delay-days 14 --out ./exports/delay-preview
```

The preview minimum is **$15,500** and ending cash remains **$35,000**. Existing output folders are refused; use a new name for every run. No package installation, browser, API key or provider request is needed for these commands.

## 2. Prepare your own inputs

Use [the complete JSON example](../examples/synthetic-scenario.json) for the schema and [Data formats](DATA-FORMAT.md) for the financial rules. Replace all fictional records, source descriptions and coverage decisions. Do not carry the example's `complete` declarations into client work without checking them.

Gather these inputs for **one entity and one currency**:

| Input | What to record and review |
| --- | --- |
| Opening cash | Exact closing cash at the stated `as_of` date, included bank accounts and dated evidence. The forecast begins the following day. Reconcile accounting cash to bank records and explain exclusions. |
| Open AR | One row per unpaid invoice, its outstanding balance, source due date, and separately reviewed expected collection date and basis. |
| Open AP | One row per unpaid bill, its outstanding balance, source due date, and separately reviewed expected payment date and basis. |
| Other cash flows | Explicit one-time assumptions and recurring payroll, rent, tax, financing and future-sales collections. Avoid counting the same receipt/payment in both open items and assumptions. |
| Coverage and provenance | Source names, observation/as-of dates, missing sections and reviewed `complete`, `partial`, `missing` or `not_applicable` decisions. |

Use decimal strings such as `"1200.00"`, ISO dates such as `2026-10-30`, and amounts **per recurring occurrence**. Do not use an invoice's original total when only part remains unpaid. A source due date is not an assumed cash date; missing timing stays visible and is excluded from scheduled cash. The tool refuses mixed currencies and performs no FX conversion.

Save your canonical inputs as, for example, `client-inputs.json`, then run:

```sh
python3.12 forecast.py --input client-inputs.json --out ./exports/client-review-01
```

Review the thirteen weekly rows, minimum cash, unscheduled items, occurrence dates and source coverage. The outputs are review-required snapshots. A delay preview does not approve or alter the baseline.

## 3. Opt into the local browser when needed

This path adds CSV import, interactive timing review, QuickBooks acquisition, approval, version comparison and Excel export. It installs the pinned packages into the extracted folder's `.venv`; first setup therefore needs internet access to PyPI.

From the extracted folder, choose a free port and a **new, explicit data directory**:

```sh
python3.12 run.py --port 8880 --data-dir ./local-data/accounting-trial
```

On Windows use `py -3.12`. Open the printed local address and keep Terminal running. This example deliberately uses a different port from the existing 8876 trial. Do not stop that process or reuse its data directory. If 8880 is occupied, select another free port and keep the explicit directory.

Check the printed source folder, data directory and address. In the browser, use **Create my entity** or **+ New entity**, choose **Entity name** and **Forecast currency**, then **Create workspace**. Open **Connections & setup** and confirm the displayed entity, address and directory before entering credentials. Different ports alone do not isolate data; connections belong to the chosen directory and entity.

For file inputs, choose **Import CSV inputs**, download its three templates, and follow their exact headers. Complete the coverage declarations and source notes, then **Validate & preview → Save this reviewed version** after reviewing the result. Native QuickBooks or Xero exports need mapping; arbitrary vendor CSV/Excel formats are not automatically supported.

A CLI `backup.json` can be restored using **Restore backup → Restore a verified backup → Validate & restore**, which creates a separate entity. Conversely, the browser's versioned backup is **not** a canonical `--input` file: its envelope contains version history, while the CLI expects the input object shown in the JSON example. Do not simply rename the backup. Pending proposals in a restored backup must be regenerated before approval.

## 4. Review before using the result

After acquisition or import, review **Inputs & timing → Review & edit**. Enter expected cash dates and timing bases, add recurring assumptions at their full amount per occurrence, and revisit coverage. Choose **Preview updated forecast**, compare its effects and select **Save this reviewed version** only after review. Scenario proposals have a separate **Approve exact changes as a new version** action. Each save/approval preserves the prior immutable version; stale proposals must be regenerated.

Export the selected version and reconcile its opening balance, weekly inflows/outflows and ending cash to the deterministic forecast. Export a versioned JSON backup for recovery; credentials are excluded. Source acquisition alone does not establish that payroll, taxes, financing or future sales are covered.

## What has been verified

The dependency-free CLI demonstration, delay preview and synthetic browser workflow have been exercised. QuickBooks uses synthetic connector fixtures in tests; **real QuickBooks sandbox/production acquisition remains unverified**. Xero has no connector in this release. Real model access, actual OS credential-store behaviour, Windows execution and broad accounting formats also remain open. See [the verification record](VERIFICATION.md) for dated evidence. No API connection was made while preparing these instructions.
