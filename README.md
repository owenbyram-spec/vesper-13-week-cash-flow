# Vesper & Co — 13-Week Cash Flow

A small local Python tool for **fractional CFOs**. Build a reproducible 13-week cash forecast and export the evidence. The default path is a command-line script; the local browser interface is optional.

Financial calculations are deterministic. The CLI needs no API keys, package installation or running server. The optional browser tool adds interactive review, immutable versions, scenario approval and an embedded assistant; AI connections require a separate opt-in.

## Run locally — CLI first

Install **Python 3.12+** from [python.org](https://www.python.org/downloads/), extract this repository, and run this from the extracted folder:

```sh
python3.12 forecast.py --demo --out ./exports/first-forecast
```

Windows: `py -3.12 forecast.py --demo --out .\exports\first-forecast` (Windows execution remains unverified). The script uses only Python's standard library. It writes exact JSON, CSV and printable HTML plus a versioned backup into a **new** output folder. Existing folders are refused, so choose a new `--out` path for another run. Running `python3.12 forecast.py` without arguments creates a separate dated demo folder automatically.

To preview a collection delay without applying it:

```sh
python3.12 forecast.py --demo --delay-days 14 --out ./exports/collection-preview
```

The baseline still ends at $35,000; the separate pending preview has minimum cash of $15,500. Nothing is approved automatically. For your own canonical input JSON, use `--input my-inputs.json`; see [data formats](docs/DATA-FORMAT.md). Every output is labelled as an input snapshot requiring review.

## Optional local browser tools

For interactive input review, CSV import, version comparison, approval, Excel export or the optional assistant, open **start.command** on macOS or **start.bat** on Windows. Terminal alternative:

```sh
python3.12 run.py
```

Only this browser path installs the pinned packages into its own `.venv` and starts a local server. Open [http://127.0.0.1:8766](http://127.0.0.1:8766) if the browser does not open. Keep the launcher running. Its synthetic workflow also needs no credentials. The CLI's `backup.json` can be restored here for review; pending proposals remain backup evidence and must be regenerated before approval.

Check the launcher's app folder, data directory and local address before entering client data or credentials. Another downloaded copy uses the same default data directory. For an isolated trial, choose both a free port and an explicit `--data-dir`; see [Quick start](docs/QUICK-START.md). Connections & setup displays the current entity, address and data directory.

## Your first useful result

1. Run the CLI demo and open its CSV or HTML report.
2. Review source coverage, expected cash dates and recurring amounts per occurrence.
3. Supply your own input JSON and choose a new output folder.
4. Opt into the local browser tool if you want interactive review and approvals.
5. Connect your own model or QuickBooks account only if you choose those additional features.

## Setup and controls

- [Quick start](docs/QUICK-START.md)
- [Model API-key setup](docs/MODEL-SETUP.md)
- [QuickBooks authorization and refresh](docs/QUICKBOOKS-SETUP.md)
- [Data formats and financial controls](docs/DATA-FORMAT.md)
- [Data flow, credentials and recovery](docs/DATA-AND-PRIVACY.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Verification and limitations](docs/VERIFICATION.md)
- [Source provenance](docs/PROVENANCE.md)
- [Design notes](docs/ARCHITECTURE.md)

## Boundaries

This is local code with an optional single-device browser utility. CLI snapshots stay in their chosen output folders; the browser has separate local workspaces. There is no shared team login, accounting writeback, Vesper-hosted backend or automatic model-provider fallback. Optional accounting refresh runs only while the browser backend is running. Optional model usage is billed to your own provider account.

Live provider access and Windows execution remain unverified; check the verification record for this build. Source repository: [vesper-13-week-cash-flow](https://github.com/owenbyram-spec/vesper-13-week-cash-flow). No software license has been added at this stage; [the release checklist](RELEASE-CHECKLIST.md) records publication verification.

## Development

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

Windows uses `.venv\Scripts\python.exe` in the same commands. Tests use synthetic records and injected credentials, not live client data.

Vesper & Co builds tailored finance workflows. Use your existing Vesper point of contact to discuss broader workflows or integrations; this repository does not collect leads or transmit financial data to Vesper.
