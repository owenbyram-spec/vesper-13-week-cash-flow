"""
Financial-expertise knowledge base — CFO-grade method records (LBO · cash-flow · close).

Sibling of ``app.assistant.asc_knowledge`` (same in-process reference-data pattern:
no DB — global, tenant-agnostic method knowledge). Distilled EXCLUSIVELY from the
crit-eval-surviving Phase-0 research corpus at
``work/cfo-workflows/research/20260706-cfo-expertise/`` (RD-1 LBO, RD-2 cash-flow,
RD-3 month-end close; plan 20260706-cfo-grade-financial-workflows, AC-1.1/1.3).

Record schema (every field non-null unless stated):

    id               int — stable record id
    domain           "lbo" | "cashflow" | "close" (see DOMAINS)
    topic            short method label
    trigger          user language / task pattern that activates the record
    guidance         the distilled ≥95%-confidence method claim
    authority_tier   "binding" | "proposed-not-binding" | "practitioner-methodology"
                     (AUTHORITY_TIERS). The asc_knowledge two-value regulatory
                     dichotomy cannot honestly label practitioner method content —
                     this three-way tier is the plan's E5 schema correction. A
                     practitioner-methodology record is NEVER rendered as
                     mandatory/regulatory; a proposed record is NEVER rendered as
                     current law (search.format_results enforces both).
    source_citation  the real citation from the RD sources index that grounds the
                     claim — never invented; refuse-over-fabricate means an
                     ungroundable claim is OMITTED, not defaulted.
    effective_date   str | None — grounded effective-date sentence for binding
                     records where the corpus recorded one; None is rendered
                     honestly ("not recorded — do not infer"), never guessed.
    accessed_date    ISO date the source was fetched/accessed per the RD.
    last_reviewed    ISO date this record's currency was last verified.

Exclusions (SYNTHESIS §4-§5.1): only ≥95%-confidence claims encode; open gaps are
excluded until their close-paths run. Phase-2 (plan 20260707-cfo-workflows-residuals,
wave R2) closed two with acquired dated citations — G1 now carries a REPRESENTATIVE
filed sweep step-down grid (record 39, cited to two SEC EX-10 credit agreements; the
per-deal posture of record 4 stands, this is an illustrative default, NOT a universal
constant), and G7 now carries the PCAOB AS 2305 replacement STATUS (record 38, tagged
proposed-not-binding — proposal issued 2024-06-12, no final standard adopted). Still
excluded: no preferred-return/catch-up market terms (G2), no numeric forecast-accuracy
THRESHOLDS (G5 — the MAPE/bias METHOD is record 25; the numeric thresholds stay OUT
because no PRIMARY dated treasury source could be fetched — only a secondary blog
attributing a member-gated AFP survey; see work/cfo-workflows/research/20260707-residuals-sources/NOTES.md),
no universal collection-haircut rates (G6).
"""
from __future__ import annotations

_LAST_REVIEWED = "2026-07-07"

# The closed authority vocabulary — asserted in tests/test_financial_expertise_kb.py.
AUTHORITY_TIERS = frozenset({"binding", "proposed-not-binding", "practitioner-methodology"})

_BINDING = "binding"
_PRACT = "practitioner-methodology"

# Retrieval domains (the analogue of asc_knowledge ROUTE_TAGS).
DOMAINS: dict[str, str] = {
    "lbo": "LBO deal mechanics — sources & uses, debt tranches, sweep, PIK, covenants, returns, rollover, add-backs, sensitivity, exit",
    "cashflow": "Cash-flow modeling — 13-week direct forecast, driver-based indirect, working-capital days, runway, scenarios, variance & accuracy",
    "close": "Month-end close — calendar, reconciliations, accruals & cut-off, flux, roll-forwards, tie-outs, controls, close-quality metrics",
}


def _r(
    id: int,
    domain: str,
    topic: str,
    trigger: str,
    guidance: str,
    *,
    tier: str,
    citation: str,
    effective_date: str | None = None,
    accessed_date: str = "2026-07-07",
    last_reviewed: str = _LAST_REVIEWED,
) -> dict:
    """Build one financial-expertise record (mirrors asc_knowledge ``_t``)."""
    return {
        "id": id,
        "domain": domain,
        "topic": topic,
        "trigger": trigger,
        "guidance": guidance,
        "authority_tier": tier,
        "source_citation": citation,
        "effective_date": effective_date,
        "accessed_date": accessed_date,
        "last_reviewed": last_reviewed,
    }


FINANCIAL_EXPERTISE_RECORDS: list[dict] = [
    # ── LBO (RD-1) ────────────────────────────────────────────────────────────
    _r(1, "lbo", "Sources & Uses — sponsor equity is the residual plug",
       "sources and uses table, size sponsor equity, entry equity, S&U balance",
       "Build Uses first (purchase enterprise value + transaction fees + financing fees + minimum "
       "cash); size each debt tranche as a multiple of LTM EBITDA; sponsor equity = total uses − "
       "total debt (− rollover / co-invest), so Sources ≡ Uses by construction. The same entry-equity "
       "number feeds the pro-forma opening balance sheet AND the returns denominator — never an "
       "independent input or a defaulted literal.",
       tier=_PRACT,
       citation="Wall Street Prep, 'Sources and Uses of Funds' + 'Basics of an LBO Model' — wallstreetprep.com (accessed 2026-07-07)"),
    _r(2, "lbo", "ASC 805 purchase accounting — goodwill is the residual",
       "purchase price allocation, opening balance sheet, goodwill plug, bargain purchase",
       "Under ASC 805 identifiable assets acquired and liabilities assumed are recognized (generally "
       "at fair value) and goodwill is the RESIDUAL — never a free input. A bargain purchase is left "
       "negative (a gain), not plugged to zero. A book-value simplification (no fair-value step-up of "
       "identifiable intangibles) must be disclosed, not silently implied.",
       tier=_BINDING,
       citation="FASB ASC 805 — asc_knowledge corpus records 136-139 (fasb.org, browser-fetched 2026-07-06)",
       accessed_date="2026-07-06"),
    _r(3, "lbo", "Debt tranching, amortization order, and the sweep circularity",
       "debt schedule, tranche, seniority, mandatory amortization, bullet maturity, circular interest",
       "Model each tranche separately (seniority rank, fixed/floating rate, mandatory amortization, "
       "bullet, PIK flag, sweep participation). Keep mandatory amortization and optional prepayment "
       "as DISTINCT lines; per-period order is cash interest → mandatory amort → optional sweep, "
       "each capped at the remaining balance, repaid senior-first. Interest on the post-sweep balance "
       "is circular — resolve by fixed-point iteration and FAIL LOUD on non-convergence.",
       tier=_PRACT,
       citation="Breaking Into Wall Street, 'Debt Schedule' + 'Cash Flow Sweep in LBO Models'; IB Interview Questions, 'LBO Debt Schedule' (accessed 2026-07-07)"),
    _r(4, "lbo", "Cash sweep — excess cash after interest, amort, and a minimum-cash floor",
       "cash sweep, excess cash flow prepayment, sweep percentage, minimum cash floor",
       "Sweep = excess cash flow AFTER cash interest, mandatory amortization, and a minimum operating-"
       "cash floor, applied to the senior-most sweep-participating tranche at the contractual sweep "
       "percentage (frequently <100%, commonly stepping down as leverage falls); never sweep the same "
       "dollar twice, and never sweep to zero operating cash. A REPRESENTATIVE filed step-down grid is "
       "record 39 (cited to SEC EX-10 credit agreements); the exact %/thresholds remain PER-DEAL "
       "negotiated — the contractual clause governs, not a universal constant.",
       tier=_PRACT,
       citation="Wall Street Prep, 'LBO Cash Sweep' — wallstreetprep.com/knowledge/cash-sweep/ (page updated 2024-02-20; fetched 2026-07-07)"),
    _r(5, "lbo", "PIK interest — the three required model legs",
       "PIK, payment in kind interest, PIK accrual, non-cash interest",
       "PIK accrues on the beginning balance and is added to principal instead of paid in cash. Three "
       "legs, all mandatory: (1) P&L — PIK is real interest expense; (2) cash flow — PIK is a non-cash "
       "add-back in CFO (else the sweep is understated); (3) balance sheet — ending debt grows by the "
       "accrual, reducing exit equity by the compounded balance.",
       tier=_PRACT,
       citation="IB Interview Questions, 'LBO Debt Schedule: Amortization, Mandatory Repayments, and Cash Sweeps'; Breaking Into Wall Street debt-schedule guides (accessed 2026-07-07)"),
    _r(6, "lbo", "ASC 835-30 — debt issuance costs presentation",
       "debt issuance costs, financing fees amortization, effective interest",
       "For book purposes debt issuance costs are presented as a direct deduction from the debt "
       "carrying amount and amortized into interest expense under the effective-interest method. A "
       "simple-interest deal-model line is a disclosed simplification — fine for deal math, wrong to "
       "present as the book interest figure.",
       tier=_BINDING,
       citation="FASB ASU 2015-03, Interest—Imputation of Interest (Subtopic 835-30) — https://storage.fasb.org/ASU%202015-03.pdf",
       effective_date="ASU 2015-03 issued April 2015 (presentation of debt issuance costs)"),
    _r(7, "lbo", "Covenant modeling — maintenance vs incurrence, covenant EBITDA, honest headroom",
       "covenants, maintenance covenant, incurrence covenant, leverage ratio headroom, covenant EBITDA",
       "Maintenance covenants test every quarter regardless of action; incurrence covenants test only "
       "on an action (new debt, dividend). Private/middle-market credit typically retains maintenance "
       "covenants even though most broadly-syndicated leveraged loans are covenant-lite. Project each "
       "metric off the PROJECTED debt schedule using EBITDA AS CONTRACTUALLY DEFINED (explicit add-back "
       "mapping, never keyword inference), report headroom % and periods-to-breach, and degrade "
       "honestly — no data ≠ pass.",
       tier=_PRACT,
       citation="Wall Street Prep, 'Covenant-Lite Loans'; IB Interview Questions, 'Debt Covenants Explained'; Federal Reserve Bank of Dallas, dallasfed.org/research/economics/2024/0820 (2024-08-20; accessed 2026-07-07)"),
    _r(8, "lbo", "ASC 470 — covenant breach can force current classification",
       "covenant violation, debt classification, callable, waiver",
       "A covenant violation at the reporting date can make the debt callable and therefore CURRENT-"
       "classified unless waived or cured. Breach detection must surface the classification "
       "consequence, not just the ratio miss.",
       tier=_BINDING,
       citation="FASB ASC 470 — asc_knowledge corpus records 78, 81 (fasb.org, browser-fetched 2026-07-06)",
       accessed_date="2026-07-06"),
    _r(9, "lbo", "Returns — IRR over dated cash flows; report the MOIC + IRR pair",
       "IRR, MOIC, annualized return, compute IRR from MOIC, hold period return",
       "IRR is solved over the DATED cash-flow vector (entry outflow, interim distributions, exit "
       "proceeds) so that NPV = 0 — never annualized from MOIC: MOIC^(1/hold)−1 is valid ONLY with "
       "zero interim flows and breaks under sweep/PIK/recap economics. Report MOIC and IRR together "
       "(magnitude AND speed), distinguish gross vs net of fees/carry at every metric, and trace the "
       "denominator to Sources & Uses.",
       tier=_PRACT,
       citation="Private Equity Bro, 'MOIC vs. IRR: When to Use Each Return Metric' (accessed 2026-07-07); ILPA Performance Template — Granular Methodology (released January 2025) — ilpa.org"),
    _r(10, "lbo", "Realized-distribution metrics — cash-on-cash / DPI (ILPA-shaped)",
       "cash on cash, DPI, distributed to paid-in, realized return, distributions",
       "Track DISTRIBUTED vs invested capital separately from unrealized NAV (DPI-style realized "
       "multiples per year). The ILPA Performance Template (released January 2025; template generation "
       "effective for funds from Q1 2026, first delivery Q1 2027) standardizes this gross/net, "
       "levered/unlevered cash-flow-level reporting — an industry standards body's voluntary/"
       "contractual template, NOT a regulation.",
       tier=_PRACT,
       citation="ILPA, 'Performance Template' + 'Performance Template Guidance — Granular Methodology' (released January 2025) — ilpa.org"),
    _r(11, "lbo", "Management rollover — a Source, a second equity class, structure-dependent tax deferral",
       "management rollover, rollover equity, seller roll, section 351, section 721",
       "Model rollover as a SOURCE that reduces sponsor cash equity (never a Use of cash) and as a "
       "separate equity class through entry (denominator split) and exit (proceeds split); typical "
       "scale ~5-20% of total equity capitalization. Tax deferral under IRC §351 (corporate, 80%-"
       "control test) or §721 (partnership) is binding law but STRUCTURE-DEPENDENT — gain recognizes "
       "on the cash portion; present deferral as advisor-confirmed, never automatic.",
       tier=_PRACT,
       citation="Wall Street Prep, 'Rollover Equity'; Breaking Into Wall Street, 'Management Rollover vs. Management Option Pool in LBOs'; Linden Law Partners, 'Rollover Equity in M&A' (accessed 2026-07-07)"),
    _r(12, "lbo", "Goodwill — ASC 350-20 book (impairment-only / ≤10y election) vs IRC §197 tax (15y)",
       "goodwill amortization, book versus tax goodwill, 15 year amortization, private company alternative",
       "Book goodwill is NOT amortized (impairment-only) unless the ASC 350-20 private-company "
       "alternative is elected — then straight-line over ≤10 years. IRC §197 amortizes goodwill "
       "ratably over 15 years FOR TAX. A 15-year life is defensible only as a tax/cash-tax proxy and "
       "must be labeled as such — never presented as GAAP book treatment.",
       tier=_BINDING,
       citation="FASB ASU 2014-02 (Topic 350, January 2014) — https://storage.fasb.org/ASU%202014-02_2.pdf; 26 U.S.C. §197 — law.cornell.edu/uscode/text/26/197 (fetched 2026-07-07)",
       effective_date="ASU 2014-02 issued January 2014 (private-company goodwill alternative); IRC §197 is current tax law"),
    _r(13, "lbo", "EBITDA add-backs — per-item provenance; covenant vs valuation EBITDA differ",
       "EBITDA add-backs, adjusted EBITDA, addback provenance, quality of earnings",
       "Every add-back carries provenance (what, why, evidence, period) — the quality-of-earnings "
       "discipline; an unexplained add-back fabricates EBITDA. Covenant EBITDA and valuation EBITDA "
       "are DIFFERENT defined terms — thread the add-back set explicitly to each consumer. Calibrated "
       "skepticism: S&P finds adjustments ≈28% of management-adjusted EBITDA at the median (2025 "
       "study), >50% for 'B'-rated credits, and larger add-backs correlate with bigger projection "
       "misses (median leverage miss ≈2.3 turns after one year) — a skepticism prior for broadly-"
       "syndicated issuers, directional below that market.",
       tier=_PRACT,
       citation="S&P Global Ratings, 'Addback Levels Remain High Despite Some Easing' (2025-04-17) + 'When The Credit Cycle Turns: The EBITDA Add-Back Fallacy' (2018-09-24) — spglobal.com"),
    _r(14, "lbo", "Sensitivity grids & exit assumptions — 2-D tables; exit equity off NET debt; exit = entry default",
       "sensitivity analysis, entry exit multiple grid, exit assumptions, multiple expansion, exit equity",
       "Run 2-D sensitivity grids over the highest-torque pairs (entry × exit multiple; leverage × "
       "EBITDA growth) and decompose returns into EBITDA growth, multiple expansion, and debt paydown. "
       "Exit equity = exit EV − transaction fees − NET debt (credit accumulated cash — gross-debt exit "
       "equity silently vaporizes trapped cash). The conservative default is exit multiple = entry so "
       "multiple expansion is never a silent return driver; scenarios move drivers coherently.",
       tier=_PRACT,
       citation="Multiple Expansion, 'Advanced LBO Modeling (Part 1)' (2020-06-13); Financial Edge, 'Sensitivity Analysis'; Wall Street Prep, 'Basics of an LBO Model' (accessed 2026-07-07)"),
    _r(39, "lbo", "Cash-sweep step-down — a representative filed leverage-based ECF grid (default, not universal)",
       "sweep percentage grid, excess cash flow step down, leverage based sweep, default sweep grid",
       "Grounding record 4's sweep mechanism with a REAL filed default: leveraged credit agreements commonly "
       "define the excess-cash-flow (ECF) sweep percentage as a leverage-based step-down. A canonical filed "
       "form (Signify Health / Barclays, EX-10.1, 2021-06-22): 50% if the First Lien Net Leverage Ratio > "
       "2.25×, 25% if ≤ 2.25× and > 2.00×, 0% if ≤ 2.00× — i.e. deleverage first, then release the sweep. This "
       "50/25/0 step-down SHAPE recurs across filings (PAR Technology / Owl Rock, EX-10.1, 2021-04-08, steps at "
       "1.50×/1.00× on ARR leverage), but the exact percentages and thresholds are PER-DEAL negotiated and the "
       "leverage measure varies (First-Lien-Net vs ARR) — use this grid as an illustrative default, never as a "
       "universal market constant; the contractual clause governs.",
       tier=_PRACT,
       citation="SEC EDGAR EX-10.1 credit agreements: Signify Health LLC / Barclays (dated 2021-06-22, accession 0001828182-21-000021) — sec.gov/Archives/edgar/data/1828182/000182818221000021/exhibit101062221.htm; PAR Technology Corp / Owl Rock (dated 2021-04-08, accession 0001140361-21-012100) — sec.gov/Archives/edgar/data/708821/000114036121012100/brhc10022897_ex10-1.htm (fetched 2026-07-07)",
       effective_date="Signify Health credit agreement dated 2021-06-22; PAR Technology credit agreement dated 2021-04-08 (representative filed grids, not a regulatory effective date)"),

    # ── Cash-flow (RD-2) ─────────────────────────────────────────────────────
    _r(15, "cashflow", "13-week direct cash forecast (TWCF) — structure and weekly ritual",
       "13 week cash flow forecast, TWCF, weekly cash grid, rolling forecast",
       "A rolling 13-week grid of weekly cash receipts less disbursements by the DIRECT method: start "
       "from today's BANK balance (not book), place each open AR item in its expected collection week "
       "(due date + entity lateness offset) and each open AP item in its expected payment week, layer "
       "recurring non-invoice flows (payroll, rent, debt service, taxes) on their actual cadence, roll "
       "ending = beginning + in − out, and refresh weekly — convert the prior week to actuals and "
       "append week 14.",
       tier=_PRACT,
       citation="Wall Street Prep, '13-Week Cash Flow Model (TWCF)' (updated 2024-11-01); Drivetrain, 13-week cash-flow forecast guide (updated 2026-03-19); Dwight Funding practical guide (accessed 2026-07-07)"),
    _r(16, "cashflow", "Direct vs indirect — the horizon split",
       "direct versus indirect forecasting, forecast method choice, medium term cash forecast",
       "Direct (dated receipts/disbursements) wins at the ≤13-week horizon because liquidity is a "
       "TIMING problem monthly indirect forecasts cannot resolve to weeks. Beyond ~13 weeks forecast "
       "indirectly: project the P&L from named drivers and derive cash as NI + D&A − ΔNWC − capex ± "
       "financing (three-statement articulation) — contracted line-item data does not exist that far "
       "out.",
       tier=_PRACT,
       citation="Atlar, 'What are direct and indirect forecasting methods?' (accessed 2026-07-07); AFP, 'What Is Cash Forecasting?' (2022-05-26)"),
    _r(17, "cashflow", "Item placement hierarchy — committed flows first, run-rate last",
       "invoice driven placement, AR collections timing, run rate fallback, double counting entities",
       "Place committed flows first (open invoices/bills — contracted data, the highest-reliability "
       "class — with per-entity behavioral lateness offsets), recurring patterns second, variable "
       "run-rate last as the fallback for entities that never generate invoices. Never count one "
       "entity in two engines: run-rate fill must skip names/months already covered by known invoices.",
       tier=_PRACT,
       citation="Drivetrain, 13-week cash-flow forecast guide (updated 2026-03-19); Dwight Funding, 'The Practical Guide to Establishing a 13-Week Cash Flow Model' (accessed 2026-07-07)"),
    _r(18, "cashflow", "ASC 230 — binding for the REPORTED statement only",
       "statement of cash flows classification, operating investing financing, restricted cash",
       "ASC 230 is the binding standard for classifying REPORTED cash flows as operating/investing/"
       "financing and reconciling to cash + equivalents (restricted cash included in the "
       "reconciliation). A FORECAST borrowing the O/I/F taxonomy is good articulation practice, but "
       "forecasting methodology itself is practitioner practice — never render TWCF/driver method "
       "guidance as a regulatory requirement.",
       tier=_BINDING,
       # PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 C3 (RK-5/AC-5) — the prior
       # citation named corpus records 184-190, which do not exist (corpus ids are contiguous
       # 1..154). Re-derived from asc_knowledge/data.py at execution time: the asc_code=="ASC 230"
       # records are 9 (statement of cash flows), 10 (restricted cash), and 17 (restricted cash —
       # presentation). Guarded by tests/test_kb_citation_integrity.py.
       citation="FASB ASC 230 — asc_knowledge corpus records 9-10, 17 with grounded fasb.org URLs (browser-fetched 2026-07-06)",
       accessed_date="2026-07-06"),
    _r(19, "cashflow", "Working-capital days — DSO / DPO / DIO / CCC formulas",
       "DSO, DPO, DIO, cash conversion cycle, working capital days, receivable days formula",
       "DSO = (AR ÷ Revenue) × 365; DPO = (AP ÷ COGS) × 365; DIO = (Inventory ÷ COGS) × 365; "
       "CCC = DIO + DSO − DPO. Forecast forward by holding/trending the DAYS and inverting to "
       "balances (AR = Revenue × DSO/365) so working-capital change is an actionable timing lever, "
       "not a %-of-revenue artifact. Guard zero/negative denominators by returning no-value — never "
       "a fabricated 0-day metric. Keep bases straight: DPO/DIO on COGS, one 365 constant.",
       tier=_PRACT,
       citation="Corporate Finance Institute, 'Cash Conversion Cycle'; Wall Street Prep, 'Cash Conversion Cycle | Formula + Calculator' (accessed 2026-07-07)"),
    _r(20, "cashflow", "Behavioral payment lateness is NOT DSO",
       "median days past due, customer payment behavior, lateness offset, mislabeled DSO",
       "median(payment_date − due_date) predicts WHEN a specific customer pays relative to due date — "
       "the right metric for weekly item placement — but it is NOT DSO and must never be labeled DSO: "
       "(AR ÷ Revenue) × days and median days-past-due answer different questions, and feeding the "
       "behavioral metric into math expecting AR-days corrupts CCC and NWC.",
       tier=_PRACT,
       citation="Corporate Finance Institute / Wall Street Prep CCC formula pages (accessed 2026-07-07); .claude/skills/financial-modeling-correctness/SKILL.md §6 (repo-internal)"),
    _r(21, "cashflow", "Liquidity runway — grid-first with a minimum-cash week; average burn as fallback",
       "runway, burn rate, minimum cash week, weeks of cash, liquidity floor",
       "When a maintained 13-week grid exists, runway is GRID-FIRST: the first week whose projected "
       "ending balance breaches zero (or the minimum-cash floor — the covenant minimum-liquidity "
       "level for levered tenants), always paired with the lowest-balance week (an average hides the "
       "mid-quarter trough). The fallback is cash ÷ average net burn over ≥3 months, financing flows "
       "excluded from operating burn, truncated conservatively (down).",
       tier=_PRACT,
       citation="Carta, 'What is a Burn Rate? How to Calculate Your Cash Runway'; Mercury, startup burn-rate guide (accessed 2026-07-07); Wall Street Prep TWCF (updated 2024-11-01)"),
    _r(22, "cashflow", "Scenario vs sensitivity — coherent named cases off one anchor",
       "stress case, scenario analysis, downside case, sensitivity ranking",
       "Sensitivity ranks one-driver-at-a-time deltas (±X days DSO, ±Y% collections) to find which "
       "lever matters; scenarios are coherent multi-driver named narratives (base/downside/shock) "
       "built around the top movers. Every case re-anchors to the SAME real bank balance, transforms "
       "drivers rather than rebuilding the model, and every scenario payload carries its case name — "
       "a uniform ×0.9 haircut is not a scenario.",
       tier=_PRACT,
       citation="Drivetrain (updated 2026-03-19); FP&A Trends, 'Driver-Based Forecasting in FP&A' (2026-01-20); Farseer, 'Top 7 Forecasting Techniques' (accessed 2026-07-07)"),
    _r(23, "cashflow", "Driver-based forecasting discipline — few, material, re-validated",
       "forecast drivers, driver sprawl, re-seed assumptions, driver based planning",
       "Seed drivers from actuals, keep them few and material, and re-validate against actuals every "
       "cycle. Documented pitfalls: over-customization (too many drivers → unmaintainable), models "
       "users don't trust (parallel spreadsheets), and scenarios that can't flow because assumptions "
       "aren't linked to drivers.",
       tier=_PRACT,
       citation="FP&A Trends (M. Karnik), 'Driver-Based Forecasting in FP&A: Common Pitfalls and Practical Solutions' (2026-01-20)"),
    _r(24, "cashflow", "Weekly variance review — timing / systematic / one-off taxonomy",
       "forecast variance to actual, variance classification, weekly variance review",
       "Compare last week's FORECAST to last week's ACTUALS per category/entity and classify each "
       "material variance: timing (self-corrects — item slipped a week; link the miss and the "
       "windfall), systematic (assumption wrong — recalibrate the driver/offset), or one-off "
       "(annotate, don't recalibrate). The taxonomy prevents both whipsaw (recalibrating on timing "
       "noise) and drift (ignoring systematic error).",
       tier=_PRACT,
       citation="Drivetrain (K. Kappelhoff), 13-week cash-flow forecast guide (updated 2026-03-19)"),
    _r(25, "cashflow", "Forecast accuracy — MAPE + bias as a pair, scored against the FROZEN forecast",
       "forecast accuracy, MAPE, bias, tracking forecast quality",
       "Score each week with paired metrics: MAPE (magnitude) AND bias = (ΣForecast − ΣActual) ÷ "
       "ΣActual (direction) — absolute error alone cannot reveal systematic optimism. Score against "
       "the FROZEN forecast-as-made N weeks ago (never the re-anchored grid, or accuracy trivially "
       "converges to zero); exclude partial-data weeks; volume-weight or fall back to absolute-$ "
       "error on near-zero denominators. No numeric accuracy THRESHOLDS are encoded — the treasury-"
       "canonical framework is gap G5 until its source is acquired. (Provenance note: these metrics "
       "are grounded in open demand-forecasting practice; they are horizon-agnostic arithmetic.)",
       tier=_PRACT,
       citation="SPS Commerce, 'Forecast Accuracy and Forecast Bias'; SupliiChain, 'Mastering Forecast Accuracy: MAPE, Bias, Tracking Signal' (accessed 2026-07-07; demand-forecasting provenance, noted)"),
    _r(26, "cashflow", "Signed amounts — QBO TotalAmt is unsigned; sign by direction before summing",
       "unsigned amounts, transaction direction, sum of bank transactions, inflated balance",
       "QBO TotalAmt is UNSIGNED (a $250 expense is stored +250.00; direction lives in the "
       "transaction type). Σ(raw amounts) counts every withdrawal as a deposit — an inflated balance "
       "that never ties out. Sign every row by direction (money-out negative, money-in positive) "
       "before any sum, tie-out, or deposit/withdrawal split.",
       tier=_PRACT,
       citation=".claude/skills/financial-modeling-correctness/SKILL.md §11 (v1.0.0, researched 2026-06-12; repo-internal, grounded to tests/test_bank_recon_signed_tieout.py)"),

    # ── Month-end close (RD-3) ───────────────────────────────────────────────
    _r(27, "close", "Close calendar & checklist — owner-assigned, day-indexed, templated",
       "month end close checklist, close calendar, close template, close task owners",
       "Run the close as a standing, owner-assigned, day-indexed task calendar (due day −2 … day +N "
       "relative to period end) instantiated per period from a reusable template, split pre-close / "
       "close / post-close; owner-per-task beats team-owned (unowned tasks slip) and day-relative "
       "dates beat fixed dates. Benchmarks (calendar days): top quartile closes in ≤4.8, median 6.4, "
       "bottom quartile ≥10.",
       tier=_PRACT,
       citation="APQC, 'Cycle Time to Perform the Monthly Close' + 'Accounting Month-End Close Checklist'; CFO.com, 'Metric of the Month: Cycle Time for Monthly Close' (accessed 2026-07-07)"),
    _r(28, "close", "Bank reconciliation — anchored tie-out; never plug the difference",
       "bank reconciliation, unexplained difference, cleared balance, statement balance",
       "Anchor on the last reconciled statement balance, mark cleared book transactions against the "
       "statement, and complete ONLY when cleared balance − statement ending balance == 0 or the "
       "residual is itemized as outstanding items. An unexplained non-zero difference BLOCKS "
       "completion — a reconciliation that always completes is not a control. Use Decimal arithmetic "
       "and signed amounts; reconcile to the independent statement, never GL-to-GL.",
       tier=_PRACT,
       citation="Journal of Accountancy, 'Best practices to improve account reconciliation' (Nov 2015); Trintech risk-based reconciliation guides (accessed 2026-07-07)"),
    _r(29, "close", "Risk-based reconciliation cadence — concentrate scrutiny where risk lives",
       "reconciliation cadence, risk based reconciliation, auto certify accounts",
       "Substantiate balance-sheet accounts on a RISK-BASED cadence: high-risk (cash, high-volume, "
       "judgment-heavy, prior failures — typically ~10% of accounts) monthly; medium quarterly; "
       "low-risk annually. Auto-certify rules for tuned low-risk zero-activity accounts are standard "
       "(40-65% auto-certification rates). Uniform monthly reconciliation of everything fails by "
       "dilution — reviewer attention is the scarce resource.",
       tier=_PRACT,
       citation="Trintech, '4 Steps to a Risk-Based Account Reconciliation Policy' + 'Stress Less with a Strong Risk-Based Reconciliation Process' (accessed 2026-07-07); Journal of Accountancy (Nov 2015)"),
    _r(30, "close", "Accruals — estimate-and-reverse, schedule-driven, aggregate materiality",
       "accruals, missing accrual, reversing entry, accrual materiality floor",
       "Recognize expenses/liabilities in the period incurred, independent of invoice or cash timing: "
       "recurring-accrual schedules generate period entries (the schedule IS the audit trail and "
       "completeness check), auto-reversing estimates self-correct, and a missing-accrual detector "
       "keys on recurring vendors with no bill this period. Monitor the AGGREGATE of below-floor "
       "items, not only per-item — fifty $90 accruals under a $100 floor is a $4.5k misstatement.",
       tier=_PRACT,
       citation="Ramp, 'Month-End Close Process: Steps, Checklist, and Best Practices'; APQC month-end close checklist (accessed 2026-07-07)"),
    _r(31, "close", "ASC 606 — revenue recognition and revenue cut-off",
       "revenue recognition, ASC 606, revenue cut-off, performance obligation",
       "Recognize revenue when (or as) performance obligations are satisfied — the ASC 606 five-step "
       "model. The most common cut-off error is booking on signature or invoice date before the "
       "performance obligation is satisfied; a cut-off scan must key on the service/performance "
       "period, not the document date.",
       tier=_BINDING,
       citation="FASB ASC 606 (ASU 2014-09) — asc_knowledge corpus records 91-104 with fasb.org ASU-list citation (browser-fetched 2026-07-06)",
       effective_date="ASU 2014-09 fully effective for all entities",
       accessed_date="2026-07-06"),
    _r(32, "close", "ASC 855 — subsequent events: recognized vs nonrecognized",
       "subsequent events, after year end, events before issuance, adjust or disclose",
       "Evaluate events between period end and report issuance: RECOGNIZED subsequent events "
       "(the condition existed at period end) adjust the statements; NONRECOGNIZED events (the "
       "condition arose after) are disclosed only. The evaluation window runs through the issuance "
       "or available-to-be-issued date. Misclassifying either direction misstates the period.",
       tier=_BINDING,
       citation="FASB ASC 855 (binding standard); accessible secondary: PwC Viewpoint §17.5 'Subsequent events' — viewpoint.pwc.com (accessed 2026-07-07)"),
    _r(33, "close", "Flux analysis — dual thresholds, two-sided, offsetting-error guard",
       "flux analysis, variance thresholds, actual versus prior, analytical review",
       "Every close, compare actual-vs-prior (P&L AND balance sheet) and actual-vs-budget; "
       "investigate a line on a DUAL threshold (a dollar floor and/or a percent floor — percent-only "
       "explodes on small bases, dollar-only scales wrong), gated two-sided on abs() (favorable "
       "variances hide errors too). Form the expectation BEFORE seeing the actual. Flux both the "
       "line and the account/GL level — two ±$20k errors netting to zero pass a line-level gate — "
       "and every tripped line gets an explanation tied to a driver, not a restatement of the number.",
       tier=_PRACT,
       citation="Forbes Finance Council, 'Building Flux And Variance Thresholds That Actually Work' (2025-11-03); Ramp, 'Flux Analysis in Accounting'; Numeric flux-analysis guide (accessed 2026-07-07)"),
    _r(34, "close", "Roll-forwards — continuity schedules with chain integrity",
       "roll forward, continuity schedule, beginning balance ties, balance movement",
       "For every carrying balance (fixed assets, accumulated depreciation, prepaids, accruals, "
       "deferred revenue, debt, equity): beginning + additions − reductions = ending, where beginning "
       "ties to the prior period's ending (chain integrity) and ending ties to the GL/TB control "
       "balance. Review the MOVEMENT, not just the ending balance; tie to the independently-posted "
       "GL, never to what the schedule itself proposed; never force a tie by adjusting the beginning "
       "balance.",
       tier=_PRACT,
       citation="Victory Inc., fixed-asset sub-ledger roll-forward tie-out (2024-08-01); Truewind, 'Rollforwards in Accounting' (accessed 2026-07-07)"),
    _r(35, "close", "Sub-ledger → GL tie-outs — same date, same basis, compare composition",
       "sub ledger tie out, GL control account, aging ties to balance sheet, control balance",
       "For each control account with a sub-ledger (AR, AP, fixed assets, inventory, deferred "
       "revenue): run the sub-ledger detail and the GL for the SAME as-of date on the SAME (accrual) "
       "basis, compare the sub-ledger total to the GL control balance, and investigate any difference "
       "to a named cause before close. Diff counts/composition, not just totals — a JE posted "
       "directly to the control account is the classic breaker only the tie-out catches.",
       tier=_PRACT,
       citation="Numeric, 'General Ledger Reconciliations: How to Conduct GL Recs' (accessed 2026-07-07); Victory Inc. (2024-08-01)"),
    _r(36, "close", "Controls & segregation — compensating controls; preparer ≠ approver ≠ lock",
       "segregation of duties, self approval, compensating controls, COSO, who approves",
       "Where headcount makes full segregation impractical (typical SMB finance teams), apply COSO "
       "2013's smaller-entity guidance: system-enforced compensating controls — independent review/"
       "sign-off, approval workflows, complete audit trails. For an agentic system the enforceable "
       "ladder is preparer (agent) ≠ approver (human) ≠ lock authority (human, explicit): proposal-"
       "first for every mutation, role-gated approval, append-only audit rows, break-glass unlock "
       "with a recorded reason. COSO is a voluntary framework; SOX 404/PCAOB attestation binds SEC "
       "registrants only — contextual for non-registrant tenants, never a tenant requirement.",
       tier=_PRACT,
       citation="COSO, Internal Control — Integrated Framework (2013), via KPMG whitepaper (assets.kpmg.com) + Baker Tilly COSO overview (accessed 2026-07-07)"),
    _r(37, "close", "Close-quality metrics — true days-to-close paired with post-close adjustments",
       "days to close, post close adjustments, close KPIs, unlock count",
       "Track a small balanced set: days-to-close = calendar days from PERIOD END to hard-lock (task "
       "latency is a different metric — do not conflate); post-close adjustment count AND amount (the "
       "'fast but wrong' detector); task on-time rate; reconciliation completion + unexplained-"
       "difference aging; break-glass unlock count (locking early then unlocking to adjust is gaming). "
       "Speed metrics are only valid paired with a quality metric. Benchmark against calendar-day "
       "figures (top quartile ≤4.8, median 6.4).",
       tier=_PRACT,
       citation="OneStream, '10 Financial Close KPIs You Should Be Tracking'; Xenett, 'Financial Close KPIs'; APQC close cycle-time benchmark (accessed 2026-07-07)"),
    _r(38, "close", "PCAOB AS 2305 replacement — PROPOSED, not adopted; the existing standard still binds",
       "PCAOB AS 2305, substantive analytical procedures standard, analytical procedures replacement, is the new SAP standard final",
       "The PCAOB's replacement of AS 2305 (Substantive Analytical Procedures) with a new standard, "
       "'Designing and Performing Substantive Analytical Procedures', is a PROPOSAL — issued for public "
       "comment on 2024-06-12 (comment deadline 2024-08-12, Rulemaking Docket 056) and NOT adopted as a "
       "final standard as of 2026-07-07. Treat it as proposed-not-binding: the EXISTING AS 2305 remains the "
       "current binding standard for an auditor's substantive analytical procedures; never render the "
       "proposed replacement as current law until a PCAOB adoption + SEC approval + effective date is confirmed.",
       tier="proposed-not-binding",
       citation="PCAOB, 'Substantive Analytical Procedures' standard-setting project page + Rulemaking Docket 056 — pcaobus.org/oversight/standards/standard-setting-research-projects/substantive-analytical-procedures (proposal issued 2024-06-12; fetched 2026-07-07)"),
]
