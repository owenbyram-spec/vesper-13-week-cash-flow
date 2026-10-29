# Design notes

## PE-native edition

Owen's clarified 2026-10-28 scope is the PE cash-flow workspace with its platform and AI foundation, excluding the broader module catalog. `pe_platform/upstream` retains pinned native backend code; `pe_platform/frontend` retains the native grid/panel and explicit local shell/transport adapters. `pe_platform/backend` supplies local identity, credentials, route scoping, source transport and preview/approval controls. The source manifest and [adoption plan](PE-PLATFORM-PLAN.md) record that boundary. Native Friday/run-rate/actuals rules are separate from the legacy exact-money input model; existing records are not recalculated. Read [native setup](PE-PLATFORM-SETUP.md) for infrastructure and verification limits.

## Legacy edition

This repository packages one useful finance workflow independently. Its duplicated local runtime is intentional: downloading either repository does not require the other tool, the internal factory, or Sprint. Changes to common runtime behavior need verification in each repository.

AI retrieves current evidence, explains and drafts through a closed tool set. Exact decimal calculations remain in deterministic code. A model cannot select a client, approve a proposal, confirm a match, read a key, execute SQL/shell, or post to accounting. Financial figures are rendered from verified tool output; unsupported model numerical prose is rejected.

Every consequential request carries the scope shown in the browser. A server-side active selection alone cannot authorize an old tab to act on a new client. Storage rechecks identity and revision atomically before financial writes. Network reads capture context before calling the provider and validate it before saving.

A source read is usable only when its controls establish coverage. Missing data stays missing; it does not become a zero balance. Multi-request vendor reads record observation windows and are never labelled atomic vendor snapshots. Bank and book sources stay independent; forecast due dates and cash timing remain distinct.

Credentials have explicit persistence choices. Session-only rotation does not depend on a working keychain. Deleting an existing saved entry requires confirmed removal; new generated workspaces can record only the nonsecret fact that no entry has been created. Finance backups never include credentials.

Local names and acceptance notes are operator inputs. This utility does not establish shared team identity or enforced preparer/approver separation. Polling exists only while the process is running.
