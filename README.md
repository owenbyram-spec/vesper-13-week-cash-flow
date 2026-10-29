# Vesper & Co — 13-Week Cash Flow

A local cash-flow workspace for **fractional CFOs**, using PE Workflow Builder's actual cash-flow and governed assistant foundation. The native platform is pinned to source commit `6667bc5b73173d83400a8d269c23f30942e4f00a`; [source manifests](pe_platform/source-manifest.json) distinguish unchanged upstream code from local adapters.

The native workspace includes the Friday-ending grid, source drivers, history-derived timing/cadence, actuals review, spreads, scenario/variance logic, immutable export snapshots, persistent assistant conversations, streaming, native tool policy/run evidence and proposal review. Local adapters provide device-owned workspaces and credentials; only the cash-flow/platform surface is exposed. Financial calculations remain deterministic. Live integrations remain unverified.

## Run the PE-native workspace

Install Python **3.12+** and Node.js **20+**, then run from this folder:

```sh
python3.12 start_platform.py --data-dir ./local-data/pe-platform
```

macOS shortcut: **start-platform.command**. Windows: **start-platform.bat** (execution unverified). First setup installs the platform's hash-locked Python dependencies into a separate `.venv-platform`, installs/builds the pinned React workspace, then opens [http://127.0.0.1:8770](http://127.0.0.1:8770). Its loopback API uses 8771. No Docker, hosted identity, billing account or shared Vesper key is needed for this local mode. Keep the launcher open; Ctrl+C stops only its own processes.

Choose the labelled synthetic workspace to review the native workflow without credentials. Enter model/QBO credentials directly in **Connections** only when opting into those providers. See [native setup and controls](docs/PE-PLATFORM-SETUP.md) for exact boundaries and verification status.

The PE edition currently uses **USD**, Friday-ending weeks and PE's inferred monthly run-rate semantics. The older CLI uses a different engine/input contract. Existing legacy versions are preserved separately; they are never silently recalculated or converted.

## Legacy exact-money CLI

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

## Legacy local browser tools

For interactive input review, CSV import, version comparison, approval, Excel export or the optional assistant, open **start.command** on macOS or **start.bat** on Windows. Terminal alternative:

```sh
python3.12 run.py
```

This legacy browser path installs its pinned packages into `.venv` and starts a local server. Open [http://127.0.0.1:8766](http://127.0.0.1:8766) if the browser does not open. Keep the launcher running. Its synthetic workflow also needs no credentials. The legacy CLI's `backup.json` can be restored here for review; pending proposals remain backup evidence and must be regenerated before approval.

Check the launcher's app folder, data directory and local address before entering client data or credentials. Another downloaded copy uses the same default data directory. For an isolated trial, choose both a free port and an explicit `--data-dir`; see [Quick start](docs/QUICK-START.md). Connections & setup displays the current entity, address and data directory.

## Your first useful result

1. Launch the PE-native workspace and load the labelled synthetic example.
2. Review its bank anchor, source drivers, Friday-ending weeks and actuals status.
3. Pull and categorize fictional transactions, inspect spreads and download a forecast.
4. Connect your own model or QuickBooks account only when choosing those features.
5. Review a model proposal's deterministic impact before approving any financial change.

## Setup and controls

- [PE-native platform setup](docs/PE-PLATFORM-SETUP.md)
- [PE adoption scope and acceptance criteria](docs/PE-PLATFORM-PLAN.md)
- [Quick start](docs/QUICK-START.md)
- [Legacy installation and file-input setup](docs/ACCOUNTING-SETUP.md)
- [Legacy model setup and provider account guidance](docs/MODEL-SETUP.md)
- [QuickBooks authorization and refresh](docs/QUICKBOOKS-SETUP.md)
- [Xero file inputs and API implementation requirements](docs/XERO-SETUP.md)
- [Data formats and financial controls](docs/DATA-FORMAT.md)
- [Data flow, credentials and recovery](docs/DATA-AND-PRIVACY.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Verification and limitations](docs/VERIFICATION.md)
- [Source provenance](docs/PROVENANCE.md)
- [Design notes](docs/ARCHITECTURE.md)

## Boundaries

This is local code with a single-device browser workspace. The native platform, legacy CLI snapshots and legacy browser use separate data stores. There is no shared team login, accounting writeback, Vesper-hosted backend or automatic model-provider fallback. Model usage is billed to your own provider account. The retained kernel has transitive shared models/services; that does not expose the broader module catalog.

Live provider access and Windows execution remain unverified; check the verification record for this build. Source repository: [vesper-13-week-cash-flow](https://github.com/owenbyram-spec/vesper-13-week-cash-flow). No software license has been added at this stage; [the release checklist](RELEASE-CHECKLIST.md) records publication verification.

QuickBooks API acquisition requires your own Intuit app/grant and a browser workspace. Xero API acquisition is not implemented; legacy file inputs remain available. Accounting access needs no model key. See [native setup](docs/PE-PLATFORM-SETUP.md) before creating provider credentials.

## Development

For the PE-native edition, first prepare the launcher above, then run:

```sh
.venv-platform/bin/python -m pytest pe_platform/tests -q -p no:cacheprovider
cd pe_platform/frontend
npm test
npm run typecheck
npm run build
```

The original engine's separate environment and tests remain:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

Windows uses `.venv\Scripts\python.exe` in the same commands. Tests use synthetic records and injected credentials, not live client data.

Vesper & Co builds tailored finance workflows. Use your existing Vesper point of contact to discuss broader workflows or integrations; this repository does not collect leads or transmit financial data to Vesper.
