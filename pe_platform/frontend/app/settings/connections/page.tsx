"use client";

import { useEffect, useState, type FormEvent } from "react";
import { apiClient } from "@/lib/api/client";
import { bootstrapLocal } from "@/lib/api/local";
import { getApiErrorMessage } from "@/lib/api/errors";
import { useAppStore } from "@/lib/store";
import { Button } from "@/components/ui/button";

interface ConnectionStatus {
    storage_warning?: string;
    model?: { configured: boolean; provider?: string; model?: string; storage_mode?: string };
    qbo?: { configured: boolean; bound_company?: { id?: string; name?: string; environment?: string } };
}
const field = "mt-1 w-full rounded border border-border bg-surfaceAlt px-3 py-2 text-sm";
export default function Connections() {
    const workspace = useAppStore((state) => state.workspace);
    const directory = useAppStore((state) => state.dataDirectory);
    const sourceRevision = useAppStore((state) => state.sourceRevision);
    const [status, setStatus] = useState<ConnectionStatus>({});
    const [busy, setBusy] = useState(false);
    const [message, setMessage] = useState("");
    const [error, setError] = useState("");
    const [provider, setProvider] = useState("openai");
    const [model, setModel] = useState("");
    const [key, setKey] = useState("");
    const [consent, setConsent] = useState(false);
    const [persistModel, setPersistModel] = useState(false);
    const [modelDirty, setModelDirty] = useState(false);
    const modelSaved = status.model?.configured === true && !modelDirty;
    const [qboDirty, setQboDirty] = useState(false);
    const qboSaved = status.qbo?.configured === true && !qboDirty;
    const [company, setCompany] = useState<{ id?: string; name?: string; environment?: string } | null>(null);
    const [qbo, setQbo] = useState({ environment: "sandbox", client_id: "", client_secret: "", realm_id: "", refresh_token: "", persist: false });
    async function reload() {
        const { data } = await apiClient.get<ConnectionStatus>("/local/connections");
        // Display only the documented non-secret status fields.
        const safe: ConnectionStatus = {
            storage_warning: typeof data.storage_warning === "string" ? data.storage_warning : "",
            model: { configured: data.model?.configured === true, provider: data.model?.provider, model: data.model?.model, storage_mode: data.model?.storage_mode },
            qbo: { configured: data.qbo?.configured === true, bound_company: data.qbo?.bound_company ? { id: data.qbo.bound_company.id, name: data.qbo.bound_company.name, environment: data.qbo.bound_company.environment } : undefined },
        };
        setStatus(safe);
        return safe;
    }
    useEffect(() => { reload().catch((failure) => setError(getApiErrorMessage(failure, "Could not load connection status."))); }, []);
    async function perform(action: () => Promise<void>) {
        if (busy) return;
        setBusy(true); setError(""); setMessage("");
        try { await action(); }
        catch (failure) { setError(getApiErrorMessage(failure, "Connection action failed. Saved financial work is preserved.")); }
        finally { setBusy(false); }
    }
    function modelChanged() { setModelDirty(true); setMessage(""); }
    function saveModel(event: FormEvent) {
        event.preventDefault();
        void perform(async () => {
            await apiClient.post("/local/connections/model", { provider, api_key: key, model, persist: persistModel, consent });
            const saved = await reload();
            if (!saved.model?.configured) throw new Error("Save was not confirmed for this workspace. Review the connection status before testing.");
            setKey(""); setModelDirty(false); setMessage("AI connection saved for this workspace. Test access next.");
        });
    }
    function saveQbo(event: FormEvent) {
        event.preventDefault();
        void perform(async () => {
            await apiClient.post("/local/connections/qbo", qbo);
            const saved = await reload();
            if (!saved.qbo?.configured) throw new Error("QuickBooks save was not confirmed for this workspace.");
            setQbo({ ...qbo, client_id: "", client_secret: "", refresh_token: "" });
            setQboDirty(false); setCompany(null); setMessage("QuickBooks connection saved. Test and confirm the company before refreshing.");
        });
    }
    return <div className="mx-auto max-w-4xl space-y-6 pb-12">
        <div><h1 className="text-2xl font-bold">Connections & local data</h1><p className="mt-2 text-textMuted">Connections belong to the displayed workspace. Enter secrets only in these masked local fields.</p></div>
        <section className="rounded-xl border border-border bg-surface p-5 text-sm"><h2 className="font-semibold">Confirm this workspace</h2><dl className="mt-3 grid gap-2 sm:grid-cols-[9rem_1fr]"><dt className="text-textMuted">Entity</dt><dd>{workspace?.name} · {workspace?.currency}{workspace?.synthetic ? " · SYNTHETIC" : ""}</dd><dt className="text-textMuted">Local address</dt><dd>{typeof window !== "undefined" ? window.location.origin : ""}</dd><dt className="text-textMuted">Data directory</dt><dd className="break-all">{directory}</dd><dt className="text-textMuted">Platform source</dt><dd className="break-all">{sourceRevision}</dd></dl></section>
        {error && <p role="alert" className="rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger">{error}</p>}
        {status.storage_warning && <div role="alert" aria-label="Credential storage notice" className="rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm"><p className="font-semibold">Credential storage notice</p><p className="mt-1">{status.storage_warning}</p></div>}
        {message && <p role="status" className="rounded-lg border border-success/30 bg-success/10 p-3 text-sm text-successText">{message}</p>}
        <section className="rounded-xl border border-border bg-surface p-5"><h2 className="text-lg font-semibold">AI assistant</h2><p className="mt-2 text-sm font-medium">{status.model?.configured ? `AI connection saved for ${workspace?.name}` : "No AI connection saved for this workspace"}</p><p className="mt-2 text-sm text-textMuted">Optional. Real requests send your prompt and authorised cash-flow evidence to your selected provider. Model usage is billed to your account. The model proposes financial changes for explicit review.</p>
            <fieldset disabled={busy}><form className="mt-4 space-y-4" onSubmit={saveModel}>
                <div className="grid gap-4 sm:grid-cols-2"><label className="text-sm">Provider<select className={field} value={provider} onChange={(event) => { setProvider(event.target.value); modelChanged(); }}><option value="openai">OpenAI</option><option value="anthropic">Anthropic</option></select></label><label className="text-sm">Model<input required className={field} value={model} placeholder="Model available to your account" onChange={(event) => { setModel(event.target.value); modelChanged(); }} autoComplete="off" /></label></div>
                <label className="block text-sm">API key<input required type="password" className={field} value={key} onChange={(event) => { setKey(event.target.value); modelChanged(); }} autoComplete="new-password" spellCheck={false} /></label>
                <label className="flex gap-2 text-sm"><input type="checkbox" checked={persistModel} onChange={(event) => { setPersistModel(event.target.checked); modelChanged(); }} />Store in OS credential store; otherwise keep only in this backend process.</label>
                <label className="flex gap-2 text-sm"><input required type="checkbox" checked={consent} onChange={(event) => { setConsent(event.target.checked); modelChanged(); }} />I authorise this external data flow for this workspace.</label>
                <div className="flex flex-wrap gap-3"><Button disabled={busy || !key || !model || !consent}>Save AI connection</Button><Button type="button" variant="outline" disabled={busy || !modelSaved} onClick={() => { void perform(async () => { const { data } = await apiClient.post("/local/connections/model/test", {}); setMessage(typeof data?.message === "string" ? data.message : "Connection test completed. A real scoped assistant response must be checked separately."); }); }}>Test AI connection</Button><Button type="button" variant="ghost" disabled={busy} onClick={() => { void perform(async () => { await apiClient.delete("/local/connections/model"); await reload(); setKey(""); setModelDirty(false); setMessage("Local AI connection removed."); }); }}>Remove connection</Button></div>
            </form></fieldset>
            <p className="mt-4 text-xs text-textMuted">Save stores the connection. Test sends a generic provider request. A real scoped assistant response is a separate check. Live access remains unverified until exercised with your own authorised credentials.</p>
        </section>
        <section id="quickbooks" className="rounded-xl border border-border bg-surface p-5"><h2 className="text-lg font-semibold">QuickBooks Online · read only</h2><p className="mt-2 text-sm text-textMuted">Use your own Intuit app and grant. Accounting acquisition requires no model key. Synthetic workspaces stay separate from real accounting records.</p>
            <fieldset disabled={busy}><form className="mt-4 grid gap-4 sm:grid-cols-2" onSubmit={saveQbo}>
                <label className="text-sm">Environment<select className={field} value={qbo.environment} onChange={(event) => { setQbo({ ...qbo, environment: event.target.value }); setQboDirty(true); setCompany(null); }}><option value="sandbox">Sandbox</option><option value="production">Production · eligible app required</option></select></label>
                {(["client_id", "client_secret", "realm_id", "refresh_token"] as const).map((name) => <label key={name} className="text-sm">{({ client_id: "Client ID", client_secret: "Client secret", realm_id: "Realm ID", refresh_token: "Refresh token" })[name]}<input required type={name === "realm_id" ? "text" : "password"} className={field} value={qbo[name]} onChange={(event) => { setQbo({ ...qbo, [name]: event.target.value }); setQboDirty(true); setCompany(null); }} autoComplete="new-password" spellCheck={false} /></label>)}
                <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={qbo.persist} onChange={(event) => { setQbo({ ...qbo, persist: event.target.checked }); setQboDirty(true); setCompany(null); }} />Store in OS credential store</label>
                <div className="flex flex-wrap gap-3 sm:col-span-2"><Button disabled={busy}>Save QBO connection</Button><Button type="button" variant="outline" disabled={busy || !qboSaved} onClick={() => { void perform(async () => { const { data } = await apiClient.post("/local/connections/qbo/test", {}); setCompany(data.company || data); setMessage("Review the returned company before confirming it."); }); }}>Test company</Button><Button type="button" variant="ghost" disabled={busy} onClick={() => { void perform(async () => { await apiClient.delete("/local/connections/qbo"); await reload(); setQboDirty(false); setCompany(null); setMessage("Local QuickBooks connection removed. Imported history remains."); }); }}>Disconnect</Button></div>
            </form></fieldset>
            {company && <div className="mt-4 rounded-lg border border-border bg-surfaceAlt p-4 text-sm"><h3 className="font-semibold">Returned company — review before binding</h3><p className="mt-2">{company.name || "Company"} · Realm {company.id} · {company.environment}</p><Button className="mt-3" disabled={busy || !company.id} onClick={() => { void perform(async () => { await apiClient.post("/local/connections/qbo/test", { confirm_company_id: company.id }); setCompany(null); await bootstrapLocal(); setMessage("Company confirmed for this workspace. Review coverage after refreshing the forecast."); }); }}>Confirm this company for the active entity</Button></div>}
        </section>
        <section className="rounded-xl border border-border bg-surface p-5 text-sm"><h2 className="font-semibold">Integration limits</h2><p className="mt-2 text-textMuted">Xero API acquisition is unimplemented. Real model/QBO access, actual credential-store behaviour, Windows execution and broad accounting formats require separate verification.</p><details className="mt-4"><summary className="cursor-pointer text-textMuted">Non-secret connection status</summary><pre className="mt-3 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(status, null, 2)}</pre></details></section>
    </div>;
}
