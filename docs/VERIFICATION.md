# 13-Week Cash Flow verification — 2026-10-28

`synthetic`: The browser journey loaded a fictional entity with dated opening cash, AR/AP and explicit recurring assumptions. Its base forecast ended at **$35,000.00**. Previewing a 14-day collection delay showed minimum cash **$15,500.00**, with no change in ending cash. Human approval created immutable V2, preserved V1 and cleared historical assistant evidence from the current-version conversation. The browser printable export showed V2 and all thirteen exact weekly rows.

`synthetic`: Independent recurrence checks include seven full $1,000 biweekly payroll occurrences, monthly anchors and week/month boundaries. The 25 extracted Sprint financial goldens remain in the passing suite. Tests reopen CSV/Excel outputs, validate JSON backup/restore and reject altered/stale/foreign proposals. QBO refresh preserves explicit manual assumptions, replaces AR/AP source rows and resets non-AR/AP coverage for renewed review. A missing-connection browser refresh displayed a recoverable message and retained V2.

`repository-observation`: The final full suite passed **89 tests**, exit 0; Ruff passed, exit 0. A competing-selection regression verifies that the state response's workspace, version and hashes are assembled under one storage lock.

## Runtime and API boundaries

`repository-observation`: Both tools are independent FastAPI/SQLite applications, with exact-money domain code and static browser UI. Initial macOS launch installed the hash-locked dependencies into each repository's own `.venv`; actual `start.command` launch and repeat launch were exercised. An occupied-port launch exited 1 with a recovery message and stopped no process. Normal test-process shutdown exited 0. No original platform services were needed.

`synthetic`: Assistant tests use provider fixtures and actual scoped tool dispatch. Browser rehearsal uses real local handlers with explicitly scripted narration and zero model calls. QBO tests use synthetic HTTP/token/report fixtures, including incomplete reads, paging, binding, rotation, revoked tokens and throttling. These checks do not establish behavior against a real Intuit company or a real model account.

`repository-observation`: Local Host/Origin/session/CSRF protections, displayed-workspace request binding, stale requests, proposal authority, credential redaction, explicit session-only fallback, locked-store recovery and polling controls are covered. Financial writes validate the captured scope inside the storage lock. The fresh critical review identified stale-tab actions and session-only token rotation issues; regression tests reproduce their corrected behavior. No real credential-store entry was read, saved or deleted during verification.

## Commands and results

Commands ran from each repository root using a separate isolated Python 3.12.13 verification environment with the pinned app/dev dependencies. The application launchers separately installed and ran the production lock in each app's own `.venv`.

```sh
python -m pytest -q -p no:cacheprovider
ruff check . --no-cache --output-format concise
sh -n start.command
./start.command --no-install --no-browser --testing --data-dir <isolated-test-directory>
```

`repository-observation`: Ruff, Python source parsing, macOS shell syntax, document links, independent Git roots, source-path imports and the authored-file allowlist passed (exit 0). The Windows launcher has real CRLF line endings and was inspected, but it has not been executed on Windows. A pattern scan found no high-confidence provider keys/private keys in the source candidates; this is a bounded scan, not a universal absence guarantee. The only test warning is the dependency's Starlette/httpx TestClient deprecation.

## External gates still open

`open`: Real model response and account access; real QBO sandbox/production authorization and report acquisition; actual OS-keychain behavior with a user's credentials; Windows execution; broad real-world accounting formats; GitHub owner and distribution-license selection; public publication. Do not treat the local commits, tests or screenshots as production, compliance, market or ownership proof.
