"use client";

import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, ChevronRight, Loader2, RefreshCw, Search, SlidersHorizontal, X } from "lucide-react";
import { associateDriverCatalog, forecastDriverStatus, type CatalogAssociation, type DriverCatalog, type DriverCatalogEntry } from "@/lib/api/driver-catalog";

export type DriverEntity = {
    row_key: string;
    qbo_id?: string | number | null;
    name: string;
    day_of_month: number | null;
    effective_day: number | null;
    avg_amount: number | null;
    effective_avg: number | null;
    amounts: Array<number | null>;
    source?: "invoice" | "pattern" | "hybrid" | "manual" | "actual";
    open_invoices?: number;
    total_open_balance?: number | null;
    known_amounts?: Array<number | null>;
    run_rate_amounts?: Array<number | null>;
    run_rate_source?: string;
    past_due_balance?: number | null;
    dso_days?: number;
    has_historical_data?: boolean;
};

export type DriverSelection = { type: "customer" | "vendor"; rowKey: string } | { type: "catalog"; entryKey: string };

type DriversDrawerProps = {
    open: boolean;
    onClose: () => void;
    customers: DriverEntity[];
    vendors: DriverEntity[];
    weekHeaders: string[];
    lookback: number;
    selected: DriverSelection | null;
    onSelect: (value: DriverSelection | null) => void;
    catalog: DriverCatalog | null;
    catalogLoading: boolean;
    catalogError: string | null;
    onRefreshCatalog: () => void;
};

const sourceLabels: Record<NonNullable<DriverEntity["source"]>, string> = {
    invoice: "Open items",
    pattern: "Recurring run-rate",
    hybrid: "Open items and recurring run-rate",
    manual: "Manual inputs",
    actual: "Actuals",
};

function money(value: number | null | undefined) {
    if (value == null || !Number.isFinite(value)) return "Unavailable";
    const amount = Math.abs(value).toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    });
    return value < 0 ? `($${amount})` : `$${amount}`;
}

function number(value: number | null | undefined) {
    return value == null || !Number.isFinite(value) ? "Unavailable" : String(value);
}

function runRateSource(value: string) {
    if (value === "open_items") return "Open items";
    if (value === "history") return "Cash history";
    return value || "Unavailable";
}

function dateLabel(value: string) {
    const date = /^\d{4}-\d{2}-\d{2}$/.test(value) ? new Date(`${value}T00:00:00`) : null;
    return date && !Number.isNaN(date.getTime())
        ? date.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
        : value;
}

function selectionKey(value: DriverSelection) {
    return value.type === "catalog" ? `catalog\u0000${value.entryKey}` : `${value.type}\u0000${value.rowKey}`;
}

function EvidenceItem({ label, value }: { label: string; value: string }) {
    return <div className="flex items-start justify-between gap-4 border-b border-border/70 py-2.5 last:border-0">
        <dt className="text-textMuted">{label}</dt>
        <dd className="max-w-[65%] break-words text-right font-medium text-text">{value}</dd>
    </div>;
}

/** Read-only inspection of source names and the displayed forecast; acquisition stays with the owning page. */
export function DriversDrawer({ open, onClose, customers, vendors, weekHeaders, lookback, selected, onSelect, catalog, catalogLoading, catalogError, onRefreshCatalog }: DriversDrawerProps) {
    const [mounted, setMounted] = useState(false);
    const titleId = useId();
    const descriptionId = useId();
    const panelRef = useRef<HTMLElement>(null);
    const overlayRef = useRef<HTMLDivElement>(null);
    const closeRef = useRef<HTMLButtonElement>(null);
    const detailsRef = useRef<HTMLHeadingElement>(null);
    const onCloseRef = useRef(onClose);
    const directoryButtons = useRef(new Map<string, HTMLButtonElement>());
    const previousSelection = useRef<string | null>(null);
    const selectedKey = selected ? selectionKey(selected) : null;
    const [search, setSearch] = useState("");
    const [pages, setPages] = useState<Record<string, number>>({});

    useEffect(() => { setMounted(true); }, []);
    useEffect(() => { onCloseRef.current = onClose; }, [onClose]);
    useEffect(() => { if (!open) { setSearch(""); setPages({}); } }, [open]);

    useEffect(() => {
        if (!open || !mounted) return;
        const panel = panelRef.current;
        const overlay = overlayRef.current;
        if (!panel || !overlay) return;
        const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        const previousOverflow = document.body.style.overflow;
        const background = Array.from(document.body.children)
            .filter((node): node is HTMLElement => node instanceof HTMLElement && node !== overlay && !["SCRIPT", "STYLE"].includes(node.tagName))
            .map((node) => ({ node, inert: node.getAttribute("inert"), hidden: node.getAttribute("aria-hidden") }));
        for (const { node } of background) {
            node.setAttribute("inert", "");
            node.setAttribute("aria-hidden", "true");
        }
        document.body.style.overflow = "hidden";

        const focusable = () => Array.from(panel.querySelectorAll<HTMLElement>(
            'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        )).filter((node) => !node.closest('[hidden], [inert], [aria-hidden="true"]'));
        const focusFirst = () => (focusable()[0] ?? panel).focus();
        const keydown = (event: KeyboardEvent) => {
            if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                onCloseRef.current();
            } else if (event.key === "Tab") {
                const targets = focusable();
                const first = targets[0] ?? panel;
                const last = targets[targets.length - 1] ?? panel;
                const active = document.activeElement;
                if (!targets.includes(active as HTMLElement) || (event.shiftKey ? active === first : active === last)) {
                    event.preventDefault();
                    (event.shiftKey ? last : first).focus();
                }
            }
        };
        const containFocus = (event: FocusEvent) => {
            if (!panel.contains(event.target as Node)) focusFirst();
        };
        document.addEventListener("keydown", keydown, true);
        document.addEventListener("focusin", containFocus, true);
        closeRef.current?.focus();

        return () => {
            document.removeEventListener("keydown", keydown, true);
            document.removeEventListener("focusin", containFocus, true);
            for (const { node, inert, hidden } of background) {
                if (inert === null) node.removeAttribute("inert"); else node.setAttribute("inert", inert);
                if (hidden === null) node.removeAttribute("aria-hidden"); else node.setAttribute("aria-hidden", hidden);
            }
            document.body.style.overflow = previousOverflow;
            if (opener?.isConnected) opener.focus();
        };
    }, [open, mounted]);

    useEffect(() => {
        if (!open || !mounted) return;
        if (selectedKey) {
            detailsRef.current?.focus();
        } else if (previousSelection.current) {
            (directoryButtons.current.get(previousSelection.current) ?? closeRef.current)?.focus();
        }
        previousSelection.current = selectedKey;
    }, [selectedKey, open, mounted]);

    if (!open || !mounted) return null;

    const associations = catalog ? associateDriverCatalog(catalog, customers, vendors, lookback, weekHeaders.length) : null;
    const selectedCatalog = selected?.type === "catalog"
        ? associations?.entries.find((item) => item.entry.key === selected.entryKey) : null;
    const row = selected?.type === "catalog"
        ? [...customers, ...vendors].find((entity) => entity === selectedCatalog?.row)
        : selected ? (selected.type === "customer" ? customers : vendors).find((entity) => entity.row_key === selected.rowKey) : null;
    const rowType = selected?.type === "catalog" ? selectedCatalog?.rowType : selected?.type;
    const query = search.trim().toLocaleLowerCase();
    const matches = (values: string[]) => !query || values.some((value) => value.toLocaleLowerCase().includes(query));
    const reviewIdentity = (entry: DriverCatalogEntry) => entry.entity_type === "unresolved" || entry.identity_basis === "unresolved" || entry.identity_basis === "unique_name";
    const statusLabel = (status: CatalogAssociation["status"]) => ({ scheduled: "In forecast · Scheduled", no_future_cash: "In forecast · No future cash", not_in_forecast: "Not in forecast", needs_review: "Needs review" })[status];
    const identityLabel = (entry: DriverCatalogEntry) => ({ gl_id: "GL source ID", typed_gl: "GL type and name", unique_name: "Name classification · review required", unresolved: "Unresolved · review required" })[entry.identity_basis];
    const pageSize = 50;
    const start = Number.isFinite(lookback) ? Math.max(0, Math.min(weekHeaders.length, Math.trunc(lookback))) : weekHeaders.length;
    const futureWeeks = weekHeaders.slice(start).map((week, offset) => ({ week, index: start + offset }));
    const weeklyWithoutRunRate = row?.source === "manual" && row.effective_avg === 0
        && futureWeeks.some(({ index }) => row.amounts[index] != null && row.amounts[index] !== 0);
    // Native manual/actual rows may include numeric defaults from the shared
    // row shape. Their presence does not establish open-item/history evidence.
    const hasOpenItemSource = row?.source === "invoice" || row?.source === "hybrid";
    const hasRunRateSource = row?.source === "pattern" || row?.source === "hybrid";
    const showKnownAmounts = hasOpenItemSource && row?.known_amounts !== undefined;
    const showRunRateAmounts = hasRunRateSource && row?.run_rate_amounts !== undefined;

    const buttonRef = (selection: DriverSelection) => (node: HTMLButtonElement | null) => {
        const key = selectionKey(selection);
        if (node) directoryButtons.current.set(key, node); else directoryButtons.current.delete(key);
    };
    const paginate = (key: string, total: number) => {
        const page = Math.max(0, Math.min(pages[key] ?? 0, Math.max(0, Math.ceil(total / pageSize) - 1)));
        return { page, from: page * pageSize, to: Math.min((page + 1) * pageSize, total) };
    };
    const pager = (key: string, label: string, total: number) => {
        const { page, from, to } = paginate(key, total);
        return total > pageSize ? <div className="flex items-center justify-between gap-2 pt-2 text-xs text-textMuted">
            <span>Showing {from + 1}–{to} of {total}</span>
            <div className="flex gap-2"><button type="button" aria-label={`Previous ${label}`} disabled={page === 0} onClick={() => setPages((prior) => ({ ...prior, [key]: page - 1 }))} className="rounded border border-border px-2 py-1 disabled:opacity-40">Previous</button><button type="button" aria-label={`Next ${label}`} disabled={to >= total} onClick={() => setPages((prior) => ({ ...prior, [key]: page + 1 }))} className="rounded border border-border px-2 py-1 disabled:opacity-40">Next</button></div>
        </div> : null;
    };
    const catalogSection = (key: string, label: string, entries: CatalogAssociation[]) => {
        const filtered = entries.filter(({ entry }) => matches([entry.name, ...entry.aliases]));
        if (!filtered.length) return null;
        const { from, to } = paginate(key, filtered.length);
        return <section aria-label={label} className="space-y-2">
            <div className="flex items-center justify-between"><h3 className="text-xs font-semibold uppercase tracking-wider text-textMuted">{label}</h3><span className="text-xs text-textMuted">{filtered.length}</span></div>
            <ul className="divide-y divide-border rounded-xl border border-border">{filtered.slice(from, to).map((association) => {
                const { entry } = association;
                const selection: DriverSelection = { type: "catalog", entryKey: entry.key };
                return <li key={entry.key}><button type="button" ref={buttonRef(selection)} aria-label={`Inspect GL ${entry.entity_type} ${entry.name} (${entry.entity_id ?? entry.key})`} onClick={() => onSelect(selection)} className="flex w-full items-start gap-3 p-3.5 text-left hover:bg-primary/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary">
                    <span className="min-w-0 flex-1"><span className="block break-words text-sm font-medium">{entry.name}{entry.active === false && <span className="ml-2 text-xs font-normal text-textMuted">Inactive</span>}</span><span className={`mt-1 block text-xs ${association.status === "needs_review" ? "text-warningText" : "text-textMuted"}`}>{statusLabel(association.status)}</span><span className="mt-1 block break-words text-xs text-textMuted">{entry.entity_id ? `Source ID ${entry.entity_id}` : identityLabel(entry)}</span></span><ChevronRight aria-hidden="true" className="mt-1 h-4 w-4 shrink-0 text-textMuted" />
                </button></li>;
            })}</ul>{pager(key, label, filtered.length)}
        </section>;
    };
    const forecastSection = (type: "customer" | "vendor", label: string, entities: DriverEntity[]) => {
        const filtered = entities.filter((entity) => matches([entity.name]));
        if (!filtered.length) return null;
        const key = `forecast-${type}`;
        const { from, to } = paginate(key, filtered.length);
        return <section aria-label={label} className="space-y-2"><div className="flex items-center justify-between"><h4 className="text-xs font-semibold text-textMuted">{label}</h4><span className="text-xs text-textMuted">{filtered.length}</span></div><ul className="divide-y divide-border rounded-xl border border-border">{filtered.slice(from, to).map((entity) => {
            const selection: DriverSelection = { type, rowKey: entity.row_key };
            return <li key={entity.row_key}><button type="button" ref={buttonRef(selection)} aria-label={`Inspect ${type} ${entity.name} driver`} onClick={() => onSelect(selection)} className="flex w-full items-start gap-3 p-3.5 text-left hover:bg-primary/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary"><span className="min-w-0 flex-1"><span className="block break-words text-sm font-medium">{entity.name}</span><span className="mt-1 block text-xs text-textMuted">{entity.source === "manual" ? "Manual assumption" : "Unlinked forecast row"} · {statusLabel(forecastDriverStatus(entity, lookback, weekHeaders.length))}</span></span><ChevronRight aria-hidden="true" className="mt-1 h-4 w-4 shrink-0 text-textMuted" /></button></li>;
        })}</ul>{pager(key, label, filtered.length)}</section>;
    };
    const coverage = catalog && <div className="space-y-2 rounded-xl border border-border bg-surfaceAlt/40 p-3 text-xs text-textMuted">
        <p className="font-semibold text-text">{catalogError ? "Retained GL read" : "General Ledger history"}</p>
        <p>{dateLabel(catalog.start_date)} – {dateLabel(catalog.end_date)}</p>
        <p className={catalog.coverage.status === "complete" ? "" : "font-medium text-warningText"}>{catalog.coverage.status === "complete" ? "Complete read" : catalog.coverage.status === "partial" ? "Partial read · names may be missing" : "Unavailable read"}</p>
        <p>{catalog.entries.length} observed identities · {catalog.coverage.named_rows} named GL lines · {catalog.coverage.unattributed_rows} unattributed lines</p>
        <p>Read {catalog.observed_at}</p>
        {catalog.coverage.issues.length > 0 && <ul className="list-disc space-y-1 pl-4">{catalog.coverage.issues.map((issue, index) => <li key={`${issue.code}:${index}`}>{issue.message}</li>)}</ul>}
    </div>;
    const forecastDetails = row ? <>
<dl className="grid grid-cols-2 gap-3">
                                <div className="rounded-xl border border-border bg-surfaceAlt/40 p-3"><dt className="text-xs text-textMuted">Day of month</dt><dd className="mt-1.5 text-lg font-semibold tabular-nums">{number(row.effective_day)}</dd></div>
                                <div className="rounded-xl border border-primary/15 bg-primary/5 p-3"><dt className="text-xs text-textMuted">Monthly run-rate</dt><dd className="mt-1.5 break-words text-lg font-semibold tabular-nums">{money(row.effective_avg)}</dd></div>
                            </dl>
                            {weeklyWithoutRunRate && <p className="rounded-lg border border-border bg-surfaceAlt/40 p-3 text-xs leading-relaxed text-textMuted">This row has weekly amounts even though its monthly run-rate is $0.00.</p>}
                            <section aria-label="Source evidence"><h4 className="text-xs font-semibold uppercase tracking-wider text-textMuted">Source evidence</h4><dl className="mt-2 text-sm">
                                <EvidenceItem label="Source" value={row.source ? sourceLabels[row.source] : "Unavailable"} />
                                {row.day_of_month !== row.effective_day && <EvidenceItem label="Source day of month" value={number(row.day_of_month)} />}
                                {row.avg_amount !== row.effective_avg && <EvidenceItem label="Source monthly average" value={money(row.avg_amount)} />}
                                {hasOpenItemSource && row.open_invoices !== undefined && <EvidenceItem label="Open source items" value={number(row.open_invoices)} />}
                                {hasOpenItemSource && row.total_open_balance !== undefined && <EvidenceItem label="Open balance" value={money(row.total_open_balance)} />}
                                {hasOpenItemSource && row.past_due_balance !== undefined && <EvidenceItem label="Past-due balance" value={money(row.past_due_balance)} />}
                                {hasRunRateSource && row.run_rate_source !== undefined && <EvidenceItem label="Run-rate source" value={runRateSource(row.run_rate_source)} />}
                                {hasRunRateSource && row.dso_days !== undefined && <EvidenceItem label="Source timing (days)" value={number(row.dso_days)} />}
                                {hasRunRateSource && row.has_historical_data !== undefined && <EvidenceItem label="Historical data available" value={row.has_historical_data ? "Yes" : "No"} />}
                            </dl></section>
                            <section aria-label="Forecast weekly amounts"><h4 className="text-xs font-semibold uppercase tracking-wider text-textMuted">Forecast weekly amounts</h4>
                                {futureWeeks.length === 0 ? <p className="mt-3 text-sm text-textMuted">No future forecast weeks are available.</p> : <div className="mt-3 overflow-x-auto rounded-lg border border-border"><table className="w-full text-xs tabular-nums"><caption className="sr-only">Future weekly amounts for {row.name}; historical lookback weeks are excluded.</caption><thead className="bg-surfaceAlt/60 text-textMuted"><tr><th scope="col" className="px-3 py-2 text-left font-medium">Week ending</th><th scope="col" className="px-3 py-2 text-right font-medium">Forecast</th>{showKnownAmounts && <th scope="col" className="px-3 py-2 text-right font-medium">Known</th>}{showRunRateAmounts && <th scope="col" className="px-3 py-2 text-right font-medium">Run-rate</th>}</tr></thead><tbody className="divide-y divide-border">{futureWeeks.map(({ week, index }) => <tr key={week}><th scope="row" className="whitespace-nowrap px-3 py-2.5 text-left font-normal"><time dateTime={week}>{dateLabel(week)}</time></th><td className="whitespace-nowrap px-3 py-2.5 text-right">{money(row.amounts[index])}</td>{showKnownAmounts && <td className="whitespace-nowrap px-3 py-2.5 text-right">{money(row.known_amounts?.[index])}</td>}{showRunRateAmounts && <td className="whitespace-nowrap px-3 py-2.5 text-right">{money(row.run_rate_amounts?.[index])}</td>}</tr>)}</tbody></table></div>}
                            </section>
    </> : null;

    return createPortal(
        <div ref={overlayRef} data-testid="cashflow-drivers-backdrop" className="fixed inset-0 z-[70] flex justify-end bg-black/25 overscroll-contain motion-reduce:transition-none"
            onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
            <section ref={panelRef} id="cashflow-drivers-drawer" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descriptionId} tabIndex={-1}
                className="flex h-[100dvh] w-full flex-col border-l border-border bg-surface text-text shadow-2xl sm:max-w-[420px] motion-reduce:transition-none">
                <header className="flex shrink-0 items-start gap-3 border-b border-border px-5 py-5">
                    <span className="rounded-lg bg-primary/10 p-2 text-primaryText"><SlidersHorizontal aria-hidden="true" className="h-4 w-4" /></span>
                    <div className="min-w-0 flex-1"><h2 id={titleId} className="text-lg font-semibold">Drivers</h2><p id={descriptionId} className="mt-1 text-xs leading-relaxed text-textMuted">Observed GL names and read-only links to this forecast.</p></div>
                    <button ref={closeRef} type="button" aria-label="Close drivers" onClick={onClose} className="rounded-lg p-2 text-textMuted hover:bg-surfaceAlt hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"><X aria-hidden="true" className="h-4 w-4" /></button>
                </header>
                <div className="min-h-0 flex-1 space-y-6 overflow-y-auto overscroll-contain px-5 py-5">
                    {catalogError && (!selected || selected.type === "catalog") && <div role="alert" className="rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs leading-relaxed text-warningText">{catalog ? "The latest GL refresh did not complete. Showing the retained directory; it may be stale. " : "GL directory unavailable. "}{catalogError}</div>}
                    {!selected ? <>
                        <div className="flex items-center justify-between gap-3"><h3 className="text-sm font-semibold">GL customer and vendor names</h3><button type="button" onClick={onRefreshCatalog} disabled={catalogLoading} className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border px-2.5 py-2 text-xs font-medium text-primaryText disabled:opacity-50"><RefreshCw aria-hidden="true" className="h-3.5 w-3.5" />Refresh GL directory</button></div>
                        <label className="block"><span className="sr-only">Search driver names and aliases</span><span className="flex items-center gap-2 rounded-lg border border-border bg-surface px-3 focus-within:ring-2 focus-within:ring-primary"><Search aria-hidden="true" className="h-4 w-4 shrink-0 text-textMuted" /><input type="search" value={search} onChange={(event) => { setSearch(event.target.value); setPages({}); }} placeholder="Search names or aliases" className="min-w-0 flex-1 bg-transparent py-2.5 text-sm outline-none" /></span></label>
                        {catalogLoading && <p role="status" className="flex items-center gap-2 text-sm text-textMuted"><Loader2 aria-hidden="true" className="h-4 w-4 animate-spin motion-reduce:animate-none" />{catalog ? "Refreshing GL directory…" : "Loading GL directory…"}</p>}
                        {coverage}
                        {!catalog && !catalogLoading && !catalogError && <p className="text-sm text-textMuted">The GL directory is unavailable. Refresh to read observed customer and vendor names.</p>}
                        {catalog && catalog.entries.length === 0 && <p className="text-sm text-textMuted">{catalog.coverage.status === "complete" ? "No customer or vendor names were observed in this GL window." : "No names were returned by this incomplete GL read."}</p>}
                        {associations && <>
                            {catalogSection("customers", "Customers", associations.entries.filter(({ entry }) => entry.entity_type === "customer" && !reviewIdentity(entry)))}
                            {catalogSection("vendors", "Vendors", associations.entries.filter(({ entry }) => entry.entity_type === "vendor" && !reviewIdentity(entry)))}
                            {catalogSection("review", "Names needing identity review", associations.entries.filter(({ entry }) => reviewIdentity(entry)))}
                            {query && !associations.entries.some(({ entry }) => matches([entry.name, ...entry.aliases])) && <p className="text-sm text-textMuted">No GL names or aliases match this search.</p>}
                        </>}
                        {(associations?.unlinkedCustomers.length || associations?.unlinkedVendors.length || (!catalog && (customers.length || vendors.length))) ? <section aria-label="Other forecast rows" className="space-y-4 border-t border-border pt-5"><div><h3 className="text-sm font-semibold">Other forecast rows</h3><p className="mt-1 text-xs leading-relaxed text-textMuted">Manual assumptions and forecast rows without a unique GL link. These are separate from the GL directory.</p></div>{forecastSection("customer", "Customer forecast rows", (associations?.unlinkedCustomers ?? customers) as DriverEntity[])}{forecastSection("vendor", "Vendor forecast rows", (associations?.unlinkedVendors ?? vendors) as DriverEntity[])}</section> : null}
                    </> : <>
                        <button type="button" onClick={() => onSelect(null)} className="inline-flex items-center gap-1.5 rounded text-sm font-medium text-primaryText hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"><ArrowLeft aria-hidden="true" className="h-4 w-4" />All drivers</button>
                        {selected.type === "catalog" ? selectedCatalog ? <>
                            <div><p className="text-xs font-medium uppercase tracking-wider text-primaryText">GL {selectedCatalog.entry.entity_type === "unresolved" ? "identity" : selectedCatalog.entry.entity_type}</p><h3 ref={detailsRef} tabIndex={-1} className="mt-2 break-words text-xl font-semibold leading-snug outline-none">{selectedCatalog.entry.name}</h3><p className="mt-2 text-sm font-medium text-primaryText">{statusLabel(selectedCatalog.status)}</p>{selectedCatalog.reason && <p className="mt-2 text-xs leading-relaxed text-textMuted">{selectedCatalog.reason}</p>}</div>
                            {coverage}
                            <section aria-label="GL identity evidence"><h4 className="text-xs font-semibold uppercase tracking-wider text-textMuted">GL identity evidence</h4><dl className="mt-2 text-sm"><EvidenceItem label="Source ID" value={selectedCatalog.entry.entity_id ?? "Unavailable"} /><EvidenceItem label="Classification" value={selectedCatalog.entry.entity_type === "customer" ? "Customer" : selectedCatalog.entry.entity_type === "vendor" ? "Vendor" : "Unresolved"} /><EvidenceItem label="Identity basis" value={identityLabel(selectedCatalog.entry)} /><EvidenceItem label="Contact status" value={selectedCatalog.entry.active === null ? "Unknown" : selectedCatalog.entry.active ? "Active" : "Inactive"} /><EvidenceItem label="First observed" value={selectedCatalog.entry.first_seen ? dateLabel(selectedCatalog.entry.first_seen) : "Unavailable"} /><EvidenceItem label="Last observed" value={selectedCatalog.entry.last_seen ? dateLabel(selectedCatalog.entry.last_seen) : "Unavailable"} /><EvidenceItem label="GL line count" value={number(selectedCatalog.entry.gl_line_count)} />{selectedCatalog.entry.aliases.length > 0 && <EvidenceItem label="Observed aliases" value={selectedCatalog.entry.aliases.join(", ")} />}</dl></section>
                            {row && <section aria-label="Forecast association" className="rounded-lg border border-primary/15 bg-primary/5 p-3 text-sm"><p className="font-medium">{selectedCatalog.matchBasis === "source_id" ? "Matched by source ID" : "Matched by name · read-only link"}</p><p className="mt-1 break-words text-textMuted">Forecast row: {row.name}</p></section>}
                            {forecastDetails}
                        </> : <div><h3 ref={detailsRef} tabIndex={-1} className="text-lg font-semibold outline-none">Driver unavailable</h3><p className="mt-2 text-sm text-textMuted">This GL identity is no longer in the current directory read.</p></div> : row ? <>
                            <div><p className="text-xs font-medium uppercase tracking-wider text-primaryText">{rowType === "customer" ? "Customer" : "Vendor"} forecast row</p><h3 ref={detailsRef} tabIndex={-1} className="mt-2 break-words text-xl font-semibold leading-snug outline-none">{row.name}</h3></div>
                            {forecastDetails}
                        </> : <div><h3 ref={detailsRef} tabIndex={-1} className="text-lg font-semibold outline-none">Driver unavailable</h3><p className="mt-2 text-sm text-textMuted">This driver is no longer in the displayed forecast.</p></div>}
                    </>}
                </div>
            </section>
        </div>, document.body,
    );
}
