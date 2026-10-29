"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Wallet, Bot, CheckSquare, Settings, Sun, Moon, Loader2 } from "lucide-react";
import { useAppStore } from "@/lib/store";
import { bootstrapLocal, createLocalDemo, createLocalWorkspace, selectLocalWorkspace } from "@/lib/api/local";
import { getApiErrorMessage } from "@/lib/api/errors";
import { AssistantPanel } from "@/components/layout/AssistantPanel";
import { useTheme } from "@/components/theme-provider";
import { Button } from "@/components/ui/button";

const links = [
    { href: "/cash-flow-forecast", label: "Cash Flow", icon: Wallet },
    { href: "/assistant", label: "Vesper Bot", icon: Bot },
    { href: "/actions", label: "Proposal review", icon: CheckSquare },
    { href: "/settings/connections", label: "Connections", icon: Settings },
];

export function LocalShell({ children }: { children: React.ReactNode }) {
    const state = useAppStore();
    const pathname = usePathname();
    const queries = useQueryClient();
    const { theme, toggleTheme } = useTheme();
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const [showCreate, setShowCreate] = useState(false);
    const [name, setName] = useState("");
    const [currency, setCurrency] = useState("USD");
    const owner = state.workspace ? `${state.workspace.id}:${state.workspace.revision}` : "none";

    useEffect(() => {
        let mounted = true;
        bootstrapLocal().catch((failure) => { if (mounted) setError(getApiErrorMessage(failure, "Could not open the local workspace.")); });
        return () => { mounted = false; };
    }, []);
    useEffect(() => { queries.clear(); }, [owner, queries]);

    async function perform(action: () => Promise<unknown>) {
        if (busy) return;
        setBusy(true); setError(null);
        try { await action(); }
        catch (failure) { setError(getApiErrorMessage(failure, "The local action failed. Your saved work is preserved.")); }
        finally { setBusy(false); }
    }
    return <div className="flex h-screen min-h-0 w-full bg-background">
        <aside className="flex w-52 shrink-0 flex-col border-r border-border bg-surface max-md:w-16">
            <Link href="/cash-flow-forecast" className="flex h-16 items-center border-b border-border px-4 text-lg font-bold tracking-wide" aria-label="Vesper home"><span className="max-md:hidden">VESPER & CO</span><Wallet className="hidden h-6 w-6 max-md:block" /></Link>
            <nav className="flex-1 space-y-1 p-3">{links.map(({ href, label, icon: Icon }) => <Link key={href} href={href} title={label} className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm ${pathname.startsWith(href) ? "bg-primary/10 text-primaryText" : "text-textMuted hover:bg-surfaceAlt"}`}><Icon className="h-5 w-5 shrink-0" /><span className="max-md:hidden">{label}</span></Link>)}</nav>
            <div className="border-t border-border p-4 text-xs text-textMuted max-md:hidden">Local workspace · your device</div>
        </aside>
        <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
            <header className="flex min-h-16 shrink-0 flex-wrap items-center gap-3 border-b border-border bg-surface px-4 py-2">
                <label className="flex items-center gap-2 text-sm"><span className="text-textMuted">Entity</span><select aria-label="Active workspace" value={state.workspace?.id ?? ""} disabled={!state.ready || busy} onChange={(event) => { const id = event.target.value; void perform(() => selectLocalWorkspace(id)); }} className="max-w-64 rounded border border-border bg-surfaceAlt px-2 py-1.5"><option value="" disabled>Select a workspace</option>{state.workspaces.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.name} · {workspace.currency}{workspace.synthetic ? " · synthetic" : ""}</option>)}</select></label>
                <Button variant="outline" size="sm" disabled={busy} onClick={() => setShowCreate(!showCreate)}>+ New entity</Button>
                <Button variant="ghost" size="sm" disabled={busy} onClick={() => { void perform(() => createLocalDemo()); }}>Load synthetic example</Button>
                <button aria-label="Toggle color theme" onClick={toggleTheme} className="ml-auto rounded-lg p-2 text-textMuted hover:bg-surfaceAlt">{theme === "dark" ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}</button>
            </header>
            {showCreate && <form className="flex flex-wrap items-end gap-3 border-b border-border bg-surfaceAlt p-4" onSubmit={(event) => { event.preventDefault(); void perform(async () => { await createLocalWorkspace(name, currency); setName(""); setShowCreate(false); }); }}>
                <label className="text-xs">Entity name<input required maxLength={150} value={name} onChange={(event) => setName(event.target.value)} className="mt-1 block rounded border border-border bg-surface px-3 py-2 text-sm" /></label>
                <label className="text-xs">Currency<select value={currency} onChange={(event) => setCurrency(event.target.value)} className="mt-1 block rounded border border-border bg-surface px-3 py-2 text-sm">{["USD"].map((value) => <option key={value}>{value}</option>)}</select></label>
                <Button disabled={busy || !name.trim()} size="sm">Create workspace</Button>
                <button type="button" className="text-sm text-textMuted" onClick={() => setShowCreate(false)}>Cancel</button>
            </form>}
            {error && <div role="alert" className="border-b border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">{error}<button className="ml-3 underline" onClick={() => { void perform(() => bootstrapLocal()); }}>Reload workspace</button></div>}
            <div className="flex min-h-0 flex-1 overflow-hidden">
                <main className="min-w-0 flex-1 overflow-auto p-6">
                    {!state.ready ? <div className="flex items-center justify-center gap-2 p-12 text-textMuted"><Loader2 className="h-5 w-5 animate-spin" />Opening local workspace…</div>
                        : !state.workspace ? <div className="mx-auto max-w-lg py-16"><h1 className="text-2xl font-bold">Start with a cash-flow workspace.</h1><p className="mt-3 text-textMuted">Create an entity for your own records, or load the labelled synthetic example to explore the forecast without any keys.</p><div className="mt-6 flex gap-3"><Button onClick={() => setShowCreate(true)}>Create my entity</Button><Button variant="outline" disabled={busy} onClick={() => { void perform(() => createLocalDemo()); }}>Load synthetic example</Button></div></div>
                            : <div key={owner} className="h-full">{children}</div>}
                </main>
                {state.ready && state.workspace && <AssistantPanel key={`${owner}:assistant:${state.assistantEpoch}`} />}
            </div>
        </div>
    </div>;
}
