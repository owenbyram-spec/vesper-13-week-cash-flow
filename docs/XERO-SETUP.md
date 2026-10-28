# Use Xero records with this tool

**A direct Xero API connection is not implemented in this release.** There is no Xero button, credential field, OAuth callback or CLI acquisition command. Creating a Xero developer app alone will not connect it. QuickBooks is the only implemented accounting API connector, through the optional browser tool.

You can use reviewed Xero exports as file inputs today. No Xero API credentials or OpenAI key are needed for that path. Start with [Accounting setup](ACCOUNTING-SETUP.md) for Python installation and CLI/browser choices.

## Working path today: export, map and review

1. In the intended Xero organisation, choose a dated closing-cash cutoff and record the organisation name, currency, report dates, included bank accounts and export time. Keep the original evidence locally. Reconcile the chosen cash total to the bank/accounting records; a balance-sheet total is not automatically available cash.
2. Export bank-balance evidence plus detailed unpaid sales invoices and bills for a consistent cutoff. Preserve invoice/bill identifiers, outstanding amounts, currencies and source due dates. An aged-receivables/payables summary by contact or bucket alone is insufficient for item-level cash timing. Check the report's filters and completeness.
3. Map the records into this tool's [canonical JSON example](../examples/synthetic-scenario.json) for the CLI, or its three [strict CSV templates](DATA-FORMAT.md) for the optional browser. Native Xero CSV/Excel exports are **not** accepted unchanged by a Xero-specific parser. Export layouts and broad accounting formats remain unverified.
4. Review expected collection/payment dates separately from source due dates. Add missing payroll, rent, tax, financing and future-sales assumptions. Retain exact decimal strings, a single currency and full amounts per recurring occurrence.
5. Run `python3.12 forecast.py --input client-inputs.json --out ./exports/xero-review-01`, or use **Import CSV inputs → Validate & preview** in the browser. Review coverage and all thirteen weeks. Compare opening cash and AR/AP totals with the source reports, then reconcile exports before using the result.

Mapping decisions:

| Xero evidence | Forecast input |
| --- | --- |
| Reviewed closing bank cash at the cutoff | `as_of`, `opening_cash`, `currency`, `opening_source` |
| Unpaid sales invoice balance | Cash item with `category: "ar"`, `direction: "in"` |
| Unpaid bill balance | Cash item with `category: "ap"`, `direction: "out"` |
| Source due date | `due_date`; do not silently copy it into expected timing |
| Reviewed expected receipt/payment and explanation | `expected_date`, `timing_basis` |
| Other one-time obligation/receipt | `category: "assumption"`, with an explicit source |
| Payroll/rent or other schedule | `recurrences`, using the amount for each occurrence |
| Reports and review decisions | `sources`, source descriptions and `coverage` |

Use the remaining unpaid balance, not an original invoice total or a report subtotal repeated alongside its detail rows. Do not assume every missing section is zero or not applicable. Today's open items cannot reconstruct historical AR/AP; obtain actual dated evidence or use a version captured at that time.

## Direct API setup: requirements for a future connector

This section is an implementation outline, **not a connection procedure executable in the current app**. Official Xero documentation was checked on **2026-10-28**. No Xero app, consent, token exchange or source acquisition was performed.

### Authorisation and organisation selection

For a distributed local desktop/CLI client, Xero specifies **Auth Code with PKCE**, which uses a client ID without embedding a shared client secret. A future implementation must create the app in [Xero My Apps](https://developer.xero.com/app/manage), implement its actual callback, register that exact URI, validate `state`, and use an S256 challenge. Xero requires HTTPS except for localhost; the current local cash-flow URL is not an implemented Xero callback. A confidential web-server app is a different flow with a protected secret. See [PKCE](https://developer.xero.com/documentation/guides/oauth2/pkce-flow/) and [OAuth overview](https://developer.xero.com/documentation/guides/oauth2/overview).

After consent, call `GET https://api.xero.com/connections`, display the authorised organisation names and require an explicit selection. Bind its **`tenantId`** to the local entity/data directory; the connection's `id` is a different identifier. Accounting requests include `Authorization: Bearer …` and `xero-tenant-id: <tenantId>`. Never silently choose the first returned organisation. See [tenant handling](https://developer.xero.com/documentation/guides/oauth2/tenants/).

### Current read scopes and access

New apps created on/after **2 March 2026** use granular scopes. Do not build a new setup guide around the old broad transaction/report scopes. Proposed minimum permissions for the bounded forecast acquisition are:

| Scope | Proposed purpose |
| --- | --- |
| `accounting.settings.read` | Organisation/account identity and currency |
| `accounting.invoices.read` | Sales invoices and bills |
| `accounting.reports.banksummary.read` | Candidate bank-balance evidence for a dated cutoff |
| `accounting.reports.balancesheet.read` | Only if a Balance Sheet read is also needed |
| `offline_access` | Only if refresh access is needed and tokens are managed securely |

No write scope, Journals endpoint or model/MCP connection is needed for this outline. Check the [official scope table](https://developer.xero.com/documentation/guides/oauth2/scopes/) and [granular-scopes FAQ](https://developer.xero.com/faq/granular-scopes). Connecting users need appropriate organisation and Connected Apps permissions; report reads also depend on Reports access. Start with the Demo Company/API Explorer independently of this tool. Check current developer connection limits, pricing and app eligibility before choosing an integration approach; do not assume unlimited free client connections. See [permissions](https://developer.xero.com/faq/permissions), [getting started](https://developer.xero.com/faq/getting-started) and [pricing/policy](https://developer.xero.com/faq/pricing-and-policy-updates).

### Tokens and deterministic acquisition

Xero access tokens last 30 minutes; unused refresh tokens expire after 60 days. Refresh returns a replacement pair which must be saved securely and atomically. Xero documents a 30-minute retry grace period for the prior refresh token. Reauthorise after expiry/revocation. A future connector must preserve workspace binding and selected session/OS-storage semantics without plaintext fallback. See [token types](https://developer.xero.com/documentation/guides/oauth2/token-types/) and [OAuth FAQ](https://developer.xero.com/faq/oauth2).

For source reads, the candidate endpoint is `GET https://api.xero.com/api.xro/2.0/Invoices`: `ACCREC` identifies sales invoices, `ACCPAY` bills, and approved status is `AUTHORISED`. Use remaining `AmountDue` in the source currency, preserve source dates and fetch/reconcile every open-item page. Optional expected/planned payment dates still require forecast review. Incremental `If-Modified-Since` alone can miss some due-date changes on partially paid transactions. See [Invoices](https://developer.xero.com/documentation/api/accounting/invoices) and [requests/responses](https://developer.xero.com/documentation/api/accounting/requests-and-responses/).

The [Reports API documentation](https://developer.xero.com/documentation/api/accounting/reports) describes BalanceSheet's supplied date as a **month-end** report. Do not assume it provides an arbitrary weekly opening-cash cutoff. BankSummary accepts `fromDate`/`toDate` and is a candidate for dated bank evidence, but its exact cutoff, account coverage and reconciliation must be verified against live reports before implementation is considered complete.

Any future connector must reject incomplete/mixed-currency acquisition, preserve manual assumptions and immutable versions, reset missing coverage for renewed review, and require explicit preview/approval. OAuth success alone will not establish those financial controls. Until that connector is implemented and verified, use the file workflow above. See [Verification](VERIFICATION.md) for the current open gates.
