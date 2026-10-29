# Small Business ASC Knowledge Map (CPA Agent)

**Purpose.** A high-level U.S. GAAP knowledge map for the CPA-oriented agent
serving small and midsize private businesses. It powers **triage, routing,
retrieval tagging, and first-pass issue spotting** — it is *not* a substitute
for the authoritative FASB Accounting Standards Codification, firm methodology,
engagement-specific judgment, or CPA review.

**Where the data lives.** The 154 ASC topics (id, asc_area, asc_code, route_tag,
common_trigger, agent_guidance, risk_level, requires_cpa_review, plus the five
regulatory-currency fields below) are encoded as plain Python in
[`data.py`](./data.py) — the codebase convention for codified reference data,
alongside the intent keywords and tool definitions. There is no DB table: ASC
guidance is **global reference data**, not company-scoped tenant data. Retrieval
lives in [`search.py`](./search.py). This Markdown file is the human-readable
companion (framing, rules, routing schema); `data.py` is the source of truth for
the topic list.

**Regulatory-currency schema (added 2026-07-06, Stream-6 audit).** Every record
carries five currency fields so the agent can tell a current topic from an
amended one and never presents a proposal as binding law. `format_results`
renders all of them on each hit:

| Field | Type | Meaning |
|---|---|---|
| `effective_date` | `str \| None` | governing effective-date sentence (PBE/non-PBE split where they differ); `None` = not yet recorded, a flag for the CPA content pass — **never guessed** |
| `source_citation` | `str \| None` | authoritative fasb.org ASU/Codification URL that grounds `effective_date`; `None` when `effective_date` is `None` (backfilled together or not at all) |
| `binding_status` | `"binding" \| "proposed-not-binding"` | closed vocabulary (`BINDING_STATUSES`). `binding` for effective GAAP; `proposed-not-binding` ONLY for a FASB proposal with no final ASU — such a record is **never rendered or described as mandatory** |
| `last_reviewed` | `str` (ISO date) | when the record's currency was last verified (drives the staleness re-check); `2026-07-06` for this pass |
| `amended_by` | `list[str]` | ASU pointers whose effect the record must track (e.g. a now-effective/early-adoptable amendment); `[]` when none |

**Grounding rule (anti-fabrication).** `effective_date` + `source_citation` are
backfilled **only** for the records Stream-6 §2 grounded to a fasb.org page — the
**14** grounded record ids are **21** (ASU 2025-05), **55** (ASU 2025-06),
**73** (ASU 2023-01), **83** (ASU 2024-04), **91** (Topic 606 core), **101**
(ASU 2025-07), **102** (ASU 2025-04), **122** (ASU 2024-01), **135** (ASU 2023-09),
**148** (ASU 2010-09), plus the four C9-added records **151** (ASU 2023-08),
**152** (ASU 2023-05), **153** (ASU 2026-01), **154** (ASU 2025-10). The other
**140** records carry `binding_status="binding"` + `last_reviewed` but a **null**
`effective_date` + `source_citation` — a flag for the CPA content pass, not an
invented date. No effective date, citation, or bindingness in `data.py` rests on
training-data recall.

**Wave 2.8 grounding (2026-07-14, plan `20260714-wave2-8-corpus-grounding`).** Two
more records inside the captured Stream-6 §2 `[R2]` Effective-Dates window were
grounded — **83** (ASC 470 convertible debt → ASU 2024-04 induced conversions) and
**122** (ASC 718 stock comp → ASU 2024-01 profits-interest scope) — each citing the
`[R2]` Effective Dates page (`_SRC_EFFECTIVE_DATES`), `last_reviewed="2026-07-14"`,
`requires_cpa_review` unchanged. Records whose ASU falls **outside** the captured
`[R2]` window, or whose ASU has **no single matching subject record** (ASU 2023-02
for the generic ASC 323 record 29; ASU 2025-03 for the generic 805/810 records; ASU
2025-09 for the broad 815 record 145), were deliberately **left null** — grounding a
generic record with a narrow ASU would mis-scope it (refuse-over-fabricate). A
maximal-grounding follow-on with a fasb.org fetch budget owns the rest.

**C9 content refresh (2026-07-06, plan 20260706-agent-path-remediation).** The
three Stream-6 STALE records were corrected — **21** (ASC 326 AR: ASU 2025-05
practical expedient + non-PBE post-balance-sheet-collection election, live
FY2026), **55** (ASC 350-40: ASU 2025-06 removes project stages, early-adoptable
now, mandatory after Dec 15 2027), **135** (ASC 740: ASU 2023-09 disclosures now
effective for non-PBEs, no longer future/conditional) — and four Stream-6
MISSING standards were added as records **151–154**: ASC 350-60 crypto assets
(ASU 2023-08, already effective), ASC 805-60 joint-venture formations
(ASU 2023-05, prospective from Jan 1 2025), ASC 505 PIK dividends on
equity-classified preferred (ASU 2026-01, final with a future mandatory date),
and ASC 832 government grants received (ASU 2025-10, final but not yet
mandatory). **Every record C9 authored or changed carries
`requires_cpa_review=True`: a human CPA pass is the sign-off gate — this corpus
is grounded drafting FOR that review, not a substitute.** Where a fact was not
grounded (e.g. ASU 2025-10 early-adoption permission) the record says so rather
than guessing.

**Important framing.** ASC topics are accounting guidance, not tax regulations.
Many small businesses do not prepare GAAP financial statements — they may use
tax basis, cash basis, modified cash basis, FRF for SMEs, or another
special-purpose framework. **Always identify the reporting basis before applying
ASC guidance.**

---

## How it is wired into the agent

| Layer | Location |
|---|---|
| Topic data (154 rows) | `app/assistant/asc_knowledge/data.py` |
| Retrieval + formatting | `app/assistant/asc_knowledge/search.py` |
| Tool schema (`search_asc_guidance`) | `app/assistant/tools.py` |
| Tool handler (`_handle_search_asc_guidance`) | `app/assistant/handlers.py` |
| Intent (`asc_guidance`) enum | `app/agent/runtime/types.py` |
| Intent keyword rules + LLM-refine prompt | `app/services/intent_classifier.py` |
| Intent → tool bundle + read-only marking | `app/agent/runtime/tool_registry.py` |
| Tests | `tests/test_asc_guidance.py` |

The `search_asc_guidance` tool is read-only and is reachable from the dedicated
`asc_guidance` intent as well as the `general` and `reporting_analysis` bundles,
so Claude can consult the index even when a request did not keyword-route to
`asc_guidance`.

---

## Agent Operating Rules

1. **Start with reporting basis.** GAAP, tax, cash, modified cash, FRF for SMEs,
   or other special-purpose framework? ASC applies only on a GAAP basis.
2. **Classify the entity.** Corporation, LLC, partnership, sole proprietor,
   nonprofit, construction contractor, franchisor, SaaS, real estate, healthcare
   practice, manufacturer, retailer, professional services.
3. **Classify the transaction.** Revenue, lease, debt, inventory, fixed asset,
   payroll, income tax, acquisition, related party, contingency, subsequent
   event, investment, consolidation.
4. **Separate recognition, measurement, presentation, and disclosure.** Don't
   stop at the journal entry — consider classification and required disclosures.
5. **Use authoritative retrieval for paragraph-level answers.** This layer
   routes and summarizes; conclusions require current authoritative ASC text or
   approved firm guidance.
6. **Flag private-company alternatives** where eligible and elected.
7. **Do not conflate book and tax.** GAAP depreciation, leases, revenue,
   impairment, and deferred taxes often differ from tax treatment.
8. **Escalate high-risk topics:** acquisitions, consolidation, derivatives,
   going concern, impairment, income-tax uncertainty, VIEs, material
   contingencies.

---

## Highest-Frequency ASC Clusters for Small Businesses

- **ASC 606** — revenue recognition, deferred revenue, contract assets/liabilities, returns, discounts, subscriptions, principal-agent.
- **ASC 842** — leases, ROU assets, lease liabilities, short-term elections, common-control leases, leasehold improvements.
- **ASC 330** — inventory costing, write-downs, obsolescence, shrinkage, consignment, goods in transit.
- **ASC 360** — fixed assets, depreciation, repairs vs improvements, disposals, long-lived asset impairment.
- **ASC 350** — intangibles, goodwill, internal-use software, website costs.
- **ASC 326** — credit losses (CECL) for receivables and other financial assets.
- **ASC 470 / 835** — debt, interest, issuance costs, covenants, modifications, effective interest.
- **ASC 450 / 460** — loss contingencies, litigation, guarantees, commitments.
- **ASC 740** — current and deferred income taxes, uncertain tax positions, pass-through.
- **ASC 850 / 855** — related parties and subsequent events.
- **ASC 235 / 250 / 275** — accounting policies, changes, estimates, risks and uncertainties.

---

## Routing Schema (`route_tag` values)

These are the canonical `route_tag` values used in `data.py` and accepted by the
`search_asc_guidance` tool's optional `route_tag` filter.

| Route tag | Primary ASC topics | Typical user language |
|---|---|---|
| `basis_and_presentation` | 105, 205, 210, 220, 230, 235, 270 | GAAP, financial statements, balance sheet, cash flow, notes |
| `accounting_changes_errors` | 250 | change in estimate, restatement, error, prior period |
| `risks_uncertainties` | 275 | concentration, key customer, estimate, uncertainty |
| `receivables_credit_losses` | 310, 326, 860 | A/R, allowance, bad debt, doubtful accounts |
| `inventory_cogs` | 330, 705 | inventory, COGS, shrinkage, obsolescence, consignment |
| `prepaids_contract_costs` | 340, 720 | prepaid, commission, setup cost, implementation cost |
| `fixed_assets` | 360, 410, 835-20 | equipment, depreciation, repair, improvement, disposal |
| `intangibles_software_goodwill` | 350 (incl. 350-60 crypto), 730, 805 | software, website, goodwill, intangible, R&D, crypto/bitcoin holdings |
| `leases` | 842 | lease, rent, ROU asset, lease liability, tenant allowance |
| `debt_interest` | 470, 835, 405, 480, 505 | loan, note, covenant, refinance, interest, issuance cost, preferred stock dividends (PIK) |
| `contingencies_commitments` | 440, 450, 460, 420 | lawsuit, claim, guarantee, commitment, restructuring |
| `revenue` | 606, 340-40, 610-20, 832 | revenue, deferred revenue, contract, subscription, refund, government grant |
| `compensation_benefits` | 710, 712, 715, 718 | payroll, bonus, PTO, severance, stock comp, 401(k) |
| `income_taxes` | 740 | tax provision, deferred tax, uncertain tax position |
| `business_combinations` | 805 (incl. 805-60 JV formations) | acquisition, purchase price allocation, earnout, joint venture formation |
| `consolidation_related_parties` | 810, 850, 323 | related party, affiliate, common control, consolidation |
| `fair_value_derivatives_fx` | 815, 820, 825, 830, 320, 321, 845 | fair value, swap, hedge, foreign currency |
| `subsequent_events` | 855 | after year-end, subsequent event, issued statements |
| `industry_specific` | 905, 910, 952, 958, 970 series, 985 | construction, franchise, nonprofit, real estate, software |

---

## Recommended Agent Response Pattern

For ASC-related answers, produce output in this order:

1. **Issue identified** — the likely ASC area and why it is triggered.
2. **Key facts needed** — only the missing facts required to conclude.
3. **High-level GAAP treatment** — recognition, measurement, presentation, disclosure.
4. **Book-tax warning** — where GAAP and tax may differ.
5. **Journal entry impact** — only when facts are sufficient.
6. **Disclosure impact** — note disclosures that may be required.
7. **Evidence to request** — contracts, invoices, loan/lease agreements, board
   minutes, tax returns, bank statements, appraisals, legal letters, management reps.
8. **Escalation flag** — whether CPA review, valuation, legal, or tax-specialist
   review is recommended (`requires_cpa_review` on the topic is the first signal).

---

## Materiality and Escalation Heuristics

Escalate / require CPA review when any of the following are present (topics with
`risk_level` of `high` or `specialist` carry `requires_cpa_review = True`):

- Going concern uncertainty, covenant default, recurring losses, liquidity pressure.
- Business combination, asset acquisition, earnout, purchase price allocation.
- Consolidation, VIE, common-control structure, related-party financing.
- Material lease portfolio or complex lease modification.
- Material revenue contracts with multiple performance obligations, variable
  consideration, or principal-agent uncertainty.
- Material inventory impairment, obsolete stock, costing-system limitations.
- Long-lived asset impairment indicators.
- Goodwill or indefinite-lived intangible impairment.
- Derivatives, swaps, caps, embedded derivatives, hedge accounting.
- Uncertain tax positions, deferred tax valuation allowance, unusual PTE elections.
- Litigation, claims, guarantees, reasonably possible material losses.
- Prior-period errors or changes in accounting principle.
- Subsequent events affecting solvency, debt, ownership, litigation, or major customers.
