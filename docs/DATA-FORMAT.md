# Forecast inputs and timing assumptions

Use UTF-8 CSV, ISO dates (`YYYY-MM-DD`) and exact decimal strings. The three CSVs have strict ordered headers:

```csv
as_of,amount,currency,source
2026-10-25,25000.00,USD,Approved dated opening-cash evidence
```

```csv
id,name,category,direction,amount,currency,due_date,expected_date,timing_basis,source
ar-101,Synthetic customer invoice,ar,in,12000.00,USD,2026-10-20,2026-10-30,Reviewed collection commitment,Synthetic AR snapshot
tax-1,Synthetic quarterly taxes,assumption,out,4500.00,USD,2027-01-15,2027-01-15,Explicit payment assumption,Synthetic CFO assumptions
```

```csv
id,name,direction,amount,currency,cadence,anchor_date,start_date,end_date,timing_basis,source
payroll,Payroll,out,1000.00,USD,biweekly,2026-10-30,2026-10-30,2027-01-24,Approved amount per payday,Payroll schedule
```

Cash-item category is `ar`, `ap`, or `assumption`. AR must use direction `in`; AP must use direction `out`. Use `assumption` for one-time taxes, financing, future sales, or other explicit cash assumptions that are separate from source receivable/payable balances. Recurring assumptions use the third CSV.

Directions are `in` and `out`; recurring cadences are weekly, biweekly and monthly. Recurring **amount means each occurrence**: a $1,000 biweekly payroll creates $1,000 on every scheduled payday. It is not a monthly average. Inspect occurrence dates and weekly totals before approval, especially month-end anchors.

Source due dates are separate from expected cash dates. Enter and review timing assumptions explicitly. Missing expected dates remain findings, not guaranteed receipts. Payroll, rent, future sales, taxes, financing and other missing obligations need reviewed coverage decisions; open invoices/bills alone are not a complete business forecast.

A QuickBooks refresh replaces prior AR/AP source rows with current open balances, preventing duplicate imported balances. It preserves `category=assumption` one-time cash items, recurring assumptions, original source ordinals/provenance, and separately reviewed timing for matching source IDs. Non-AR/AP coverage returns to `missing` so you review retained assumptions against the fresh as-of date. A retained one-time assumption whose expected date is on or before the new opening cash remains a visible finding and is omitted until reviewed, preventing double counting. Partial accounting acquisitions preserve the previous valid version.

Exactly thirteen explicit weeks roll forward opening cash + inflows − outflows. Overdue, beyond-horizon and unplaced items remain visible. Mixed currencies and invalid/nonfinite/excess-precision amounts are refused; this build does not convert currencies.

Input/assumption/forecast versions are immutable. A scenario proposal refers to the exact base-version hash and shows before/after impact. Acceptance creates a new version; rejection changes nothing; stale proposals must be regenerated. Captured versions support historical comparison; current QuickBooks balances cannot reconstruct historical AR/AP.

CSV/Excel and printable reports include the selected version and source/assumption context. JSON backup/restore preserves exact decimal values and provenance and excludes credentials. All shipped financial examples are synthetic.
