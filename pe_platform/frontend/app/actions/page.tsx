"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { apiClient } from "@/lib/api/client";
import { getApiErrorMessage } from "@/lib/api/errors";
import { Button } from "@/components/ui/button";
import { useAppStore } from "@/lib/store";

interface Proposal {
    id: string;
    action_type: string;
    status: string;
    payload: Record<string, unknown>;
    created_at?: string;
    base_fingerprint?: string;
    current_fingerprint?: string;
    stale: boolean;
    review_fingerprint?: string;
    summary?: string;
    review?: {
        before_state?: unknown;
        after_state?: unknown;
        approval_effect?: string;
    };
}

interface Preview {
    available: boolean;
    before?: unknown;
    after?: unknown;
    comparison?: unknown;
    reason?: string;
    review_fingerprint?: string;
    preview_token?: string;
}
interface WeeklyForecast {
    week_headers: string[];
    beginning_balances: Array<number | null>;
    total_cash_in: Array<number | null>;
    total_cash_out: Array<number | null>;
    ending_balances: Array<number | null>;
}
function weeklyForecast(value: unknown): WeeklyForecast | null {
    if (!value || typeof value !== "object") return null;
    const envelope = value as Record<string, unknown>;
    const candidate = (envelope.forecast ?? envelope) as Record<string, unknown>;
    const headers = candidate.week_headers;
    if (!Array.isArray(headers) || !headers.length || headers.some((item) => typeof item !== "string")) return null;
    for (const key of ["beginning_balances", "total_cash_in", "total_cash_out", "ending_balances"]) {
        const rows = candidate[key];
        if (!Array.isArray(rows) || rows.length !== headers.length || rows.some((item) => item !== null && (typeof item !== "number" || !Number.isFinite(item)))) return null;
    }
    return candidate as unknown as WeeklyForecast;
}
function ForecastImpact({ before, after, currency }: { before: WeeklyForecast; after: WeeklyForecast; currency: string }) {
    const money = (value: number | null) => value == null ? "—" : value.toLocaleString("en-US", { style: "currency", currency, minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return <div className="mt-4 overflow-x-auto rounded-lg border border-border"><table className="w-full whitespace-nowrap text-right text-xs"><thead className="bg-surfaceAlt"><tr><th className="p-2 text-left" rowSpan={2}>Week ending</th><th className="p-2 text-center" colSpan={4}>Current forecast</th><th className="p-2 text-center" colSpan={4}>Proposed forecast</th></tr><tr>{["Beginning", "Cash in", "Cash out", "Ending", "Beginning", "Cash in", "Cash out", "Ending"].map((label, index) => <th key={index} className="p-2">{label}</th>)}</tr></thead><tbody>{before.week_headers.map((week, index) => <tr key={week} className="border-t border-border"><th className="p-2 text-left font-normal">{week}</th>{[before, after].flatMap((forecast, side) => ["beginning_balances", "total_cash_in", "total_cash_out", "ending_balances"].map((key) => <td key={`${side}:${key}`} className="p-2 font-mono">{money(forecast[key as keyof Omit<WeeklyForecast, "week_headers">][index])}</td>))}</tr>)}</tbody></table></div>;
}

export default function ProposalReview() {
    const workspace = useAppStore((state) => state.workspace);
    const [proposals, setProposals] = useState<Proposal[]>([]);
    const [selected, setSelected] = useState<Proposal | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [message, setMessage] = useState("");
    const [confirmed, setConfirmed] = useState(false);
    const [preview, setPreview] = useState<Preview | null>(null);
    const reload = useCallback(async () => {
        const { data } = await apiClient.get<{ proposals: Proposal[] }>("/local/proposals");
        setProposals(data.proposals);
        return data.proposals;
    }, []);
    useEffect(() => { reload().catch((failure) => setError(getApiErrorMessage(failure, "Could not load proposals. An empty list has not been confirmed."))); }, [reload]);
    async function inspect(proposal: Proposal) {
        setBusy(true); setError(""); setConfirmed(false); setSelected(null); setPreview(null);
        try {
            const { data } = await apiClient.get<Proposal>(`/local/proposals/${encodeURIComponent(proposal.id)}`);
            setSelected(data);
            if (data.status.toLowerCase() === "pending" && !data.stale && data.review_fingerprint) {
                const response = await apiClient.post<Preview>(`/local/proposals/${encodeURIComponent(data.id)}/preview`, { review_fingerprint: data.review_fingerprint });
                setPreview(response.data);
            }
        } catch (failure) { setError(getApiErrorMessage(failure, "Could not load the exact proposal. Refresh and review it again.")); }
        finally { setBusy(false); }
    }
    async function decide(approve: boolean) {
        if (!selected?.review_fingerprint || busy || (approve && (!confirmed || selected.stale || !previewReady))) return;
        const reviewed = selected;
        setBusy(true); setError(""); setMessage("");
        try {
            await apiClient.post(`/local/proposals/${encodeURIComponent(reviewed.id)}/${approve ? "approve" : "reject"}`, { review_fingerprint: approve ? preview?.review_fingerprint : reviewed.review_fingerprint, ...(approve ? { preview_token: preview?.preview_token } : {}) });
            const items = await reload();
            setSelected(items.find((proposal) => proposal.id === reviewed.id) ?? null);
            setConfirmed(false); setPreview(null);
            setMessage(approve ? "Approval request completed. Review the recorded proposal status and refreshed forecast before using the result." : "Proposal rejected. Review the recorded status below.");
        } catch (failure) { setError(getApiErrorMessage(failure, "The decision could not be confirmed. Refresh the proposal before taking another action.")); }
        finally { setBusy(false); }
    }
    const pending = selected?.status.toLowerCase() === "pending";
    const before = weeklyForecast(preview?.before);
    const after = weeklyForecast(preview?.after);
    const matchingWeeks = before && after && JSON.stringify(before.week_headers) === JSON.stringify(after.week_headers);
    const previewReady = preview?.available === true && Boolean(preview.preview_token && preview.review_fingerprint && matchingWeeks);
    return <div className="mx-auto max-w-5xl space-y-5 pb-12">
        <div className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="text-2xl font-bold">Proposal review</h1><p className="mt-2 text-sm text-textMuted">Review the native cash-flow action and its source binding before approving. The assistant cannot approve on your behalf.</p></div><Button variant="outline" disabled={busy} onClick={() => { setSelected(null); setPreview(null); setConfirmed(false); setError(""); void reload().catch((failure) => setError(getApiErrorMessage(failure, "Could not reload proposals."))); }}>Refresh reviews</Button></div>
        <p className="rounded-lg border border-border bg-surface p-3 text-sm">Workspace: <strong>{workspace?.name}</strong> · {workspace?.currency}</p>
        {error && <p role="alert" className="rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger">{error}</p>}
        {message && <p role="status" className="rounded-lg border border-border bg-surfaceAlt p-3 text-sm">{message}</p>}
        <div className="grid gap-5 lg:grid-cols-[17rem_1fr]">
            <section className="space-y-2"><h2 className="text-sm font-semibold text-textMuted">Saved proposals</h2>{!proposals.length && !error ? <p className="rounded-lg border border-border bg-surface p-4 text-sm text-textMuted">No proposals are saved for this workspace. Ask the assistant to draft a cash-flow change.</p> : proposals.map((proposal) => <button key={proposal.id} disabled={busy} onClick={() => { void inspect(proposal); }} className={`block w-full rounded-lg border p-4 text-left ${selected?.id === proposal.id ? "border-primary bg-primary/5" : "border-border bg-surface hover:bg-surfaceAlt"}`}><span className="block text-xs uppercase text-textMuted">{proposal.status}{proposal.stale ? " · stale" : ""}</span><span className="mt-1 block text-sm font-semibold">{proposal.summary || proposal.action_type.replaceAll("_", " ")}</span><span className="mt-2 block break-all text-xs text-textMuted">{proposal.id}</span></button>)}</section>
            <section className="rounded-xl border border-border bg-surface p-5">{!selected ? <p className="py-12 text-center text-textMuted">Select a proposal to inspect its exact changes.</p> : <>
                <div className="flex flex-wrap justify-between gap-2"><h2 className="text-lg font-semibold">{selected.summary || selected.action_type.replaceAll("_", " ")}</h2><span className="text-sm font-medium text-textMuted">{selected.status}</span></div>
                {selected.stale && <p role="alert" className="mt-4 rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm text-warningText">The financial inputs changed after this proposal was prepared. Regenerate it against the current forecast; approval is blocked.</p>}
                <div className="mt-4 text-sm"><h3 className="font-semibold">Approval effect</h3><p className="mt-1 text-textMuted">{selected.review?.approval_effect || "The backend rechecks this exact cash-flow proposal and its financial source binding before applying an approved internal action."}</p></div>
                {previewReady && before && after ? <><h3 className="mt-5 text-sm font-semibold">Deterministic forecast impact</h3><ForecastImpact before={before} after={after} currency={workspace?.currency || "USD"} />{preview?.comparison != null && <details className="mt-3 text-xs text-textMuted"><summary className="cursor-pointer">Calculated comparison</summary><pre className="mt-2 overflow-auto whitespace-pre-wrap">{JSON.stringify(preview.comparison, null, 2)}</pre></details>}</> : <p role="status" className="mt-4 rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm text-warningText">{busy ? "Calculating the exact forecast preview…" : preview?.reason || (preview?.available ? "The returned forecast preview is incomplete. Approval is disabled; refresh and review again." : "A deterministic forecast preview is unavailable. Approval stays disabled until a complete preview can be reviewed.")}</p>}
                <h3 className="mt-5 text-sm font-semibold">Exact proposed assumptions</h3><pre className="mt-2 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-surfaceAlt p-4 text-xs">{JSON.stringify(selected.payload, null, 2)}</pre>
                <details className="mt-4 text-xs text-textMuted"><summary className="cursor-pointer">Source binding and review identity</summary><dl className="mt-2 space-y-2 break-all"><dt>Prepared from</dt><dd>{selected.base_fingerprint || "Unknown"}</dd><dt>Current financial source</dt><dd>{selected.current_fingerprint || "Unknown"}</dd><dt>Exact review fingerprint</dt><dd>{selected.review_fingerprint || "Unavailable — decision disabled"}</dd></dl></details>
                {pending && <div className="mt-6 border-t border-border pt-4"><label className="flex items-start gap-2 text-sm"><input type="checkbox" disabled={busy || selected.stale || !previewReady} checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />I reviewed this exact proposal for {workspace?.name} and authorise its stated effect.</label><div className="mt-4 flex flex-wrap gap-3"><Button disabled={busy || !confirmed || selected.stale || !previewReady || !selected.review_fingerprint} onClick={() => { void decide(true); }}>Approve exact proposal</Button><Button variant="outline" disabled={busy || !selected.review_fingerprint} onClick={() => { void decide(false); }}>Reject proposal</Button><Link href="/cash-flow-forecast" className="inline-flex items-center text-sm text-primaryText underline">Review forecast</Link></div></div>}
            </>}</section>
        </div>
    </div>;
}
