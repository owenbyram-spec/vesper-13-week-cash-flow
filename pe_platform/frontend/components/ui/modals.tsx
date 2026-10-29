"use client";

import { useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";

// Shared, accessible-ish in-app modals replacing native prompt()/confirm().
// Uses the app's established overlay pattern (fixed inset-0 z-50 backdrop).

const overlay = "fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4";
const panel = "w-full max-w-md rounded-xl border border-border bg-surface p-5 shadow-xl space-y-4";
const inputCls =
    "w-full px-3 py-2 text-sm bg-surfaceAlt border border-border rounded-lg text-text placeholder:text-textMuted focus:outline-none focus:ring-1 focus:ring-primary";
const cancelBtn = "px-4 py-2 rounded-lg border border-border text-textMuted text-sm hover:text-text";

export interface FormField {
    key: string;
    label: string;
    placeholder?: string;
    type?: "text" | "number";
    optional?: boolean;
    defaultValue?: string;
}

/** Multi-field input dialog. Replaces prompt(). onSubmit receives trimmed values keyed by field.key. */
export function FormDialog({
    open,
    title,
    description,
    fields,
    submitText = "Save",
    onSubmit,
    onClose,
}: {
    open: boolean;
    title: string;
    description?: string;
    fields: FormField[];
    submitText?: string;
    onSubmit: (values: Record<string, string>) => void;
    onClose: () => void;
}) {
    const [values, setValues] = useState<Record<string, string>>({});
    const firstRef = useRef<HTMLInputElement>(null);

    useEffect(() => {
        if (open) {
            setValues(Object.fromEntries(fields.map((f) => [f.key, f.defaultValue ?? ""])));
            // focus first field shortly after mount
            const t = setTimeout(() => firstRef.current?.focus(), 30);
            return () => clearTimeout(t);
        }
    }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

    if (!open) return null;

    const requiredMissing = fields.some((f) => !f.optional && !(values[f.key] ?? "").trim());

    const submit = () => {
        if (requiredMissing) return;
        const trimmed = Object.fromEntries(Object.entries(values).map(([k, v]) => [k, (v ?? "").trim()]));
        onSubmit(trimmed);
    };

    return (
        <div className={overlay} onMouseDown={onClose}>
            <div className={panel} onMouseDown={(e) => e.stopPropagation()}>
                <div>
                    <h3 className="text-base font-semibold text-text">{title}</h3>
                    {description && <p className="text-sm text-textMuted mt-1">{description}</p>}
                </div>
                <div className="space-y-3">
                    {fields.map((f, i) => (
                        <div key={f.key}>
                            <label className="text-xs text-textMuted block mb-1">
                                {f.label}
                                {f.optional && <span className="text-textMuted"> (optional)</span>}
                            </label>
                            <input
                                ref={i === 0 ? firstRef : undefined}
                                type={f.type ?? "text"}
                                value={values[f.key] ?? ""}
                                placeholder={f.placeholder}
                                onChange={(e) => setValues((prev) => ({ ...prev, [f.key]: e.target.value }))}
                                onKeyDown={(e) => {
                                    if (e.key === "Enter") { e.preventDefault(); submit(); }
                                    if (e.key === "Escape") onClose();
                                }}
                                className={inputCls}
                            />
                        </div>
                    ))}
                </div>
                <div className="flex justify-end gap-2 pt-1">
                    <button onClick={onClose} className={cancelBtn}>Cancel</button>
                    <Button
                        onClick={submit}
                        disabled={requiredMissing}
                        className="px-4 py-2 rounded-lg text-sm font-medium disabled:opacity-50"
                    >
                        {submitText}
                    </Button>
                </div>
            </div>
        </div>
    );
}

/** Confirmation dialog. Replaces confirm(). Set requireText to force the user to type a phrase
 * (e.g. "DELETE") for destructive actions, so they can't be triggered with a single click. */
export function ConfirmDialog({
    open,
    title,
    description,
    confirmText = "Confirm",
    destructive = false,
    requireText,
    onConfirm,
    onClose,
}: {
    open: boolean;
    title: string;
    description?: React.ReactNode;
    confirmText?: string;
    destructive?: boolean;
    requireText?: string;
    onConfirm: () => void;
    onClose: () => void;
}) {
    const [typed, setTyped] = useState("");
    useEffect(() => { if (open) setTyped(""); }, [open]);
    if (!open) return null;

    const blocked = !!requireText && typed.trim() !== requireText;

    return (
        <div className={overlay} onMouseDown={onClose}>
            <div className={panel} onMouseDown={(e) => e.stopPropagation()}>
                <div className="flex items-start gap-3">
                    {destructive && <AlertTriangle className="h-5 w-5 text-danger shrink-0 mt-0.5" />}
                    <div className="flex-1">
                        <h3 className="text-base font-semibold text-text">{title}</h3>
                        {description && <div className="text-sm text-textMuted mt-1">{description}</div>}
                    </div>
                </div>
                {requireText && (
                    <div>
                        <label className="text-xs text-textMuted block mb-1">
                            Type <span className="font-mono font-semibold text-text">{requireText}</span> to confirm
                        </label>
                        <input
                            autoFocus
                            value={typed}
                            onChange={(e) => setTyped(e.target.value)}
                            onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}
                            className={inputCls}
                        />
                    </div>
                )}
                <div className="flex justify-end gap-2 pt-1">
                    <button onClick={onClose} className={cancelBtn}>Cancel</button>
                    <Button
                        onClick={onConfirm}
                        disabled={blocked}
                        variant={destructive ? "destructive" : "default"}
                        className="px-4 py-2 text-sm"
                    >
                        {confirmText}
                    </Button>
                </div>
            </div>
        </div>
    );
}

export type SaveState = "idle" | "saving" | "saved" | "error";

/** Inline save-status affordance: Saving… / All changes saved / Save failed + Retry.
 * Matches the Accounting Schedules indicator for cross-module consistency. */
export function SaveStatus({
    state,
    error,
    onRetry,
    className = "",
}: {
    state: SaveState;
    error?: string | null;
    onRetry?: () => void;
    className?: string;
}) {
    if (state === "idle") return null;
    return (
        <div className={`flex items-center gap-2 text-xs ${className}`}>
            {state === "saving" && (
                <span className="flex items-center gap-1.5 text-textMuted"><RefreshCw className="w-3 h-3 animate-spin" /> Saving…</span>
            )}
            {state === "saved" && (
                <span className="flex items-center gap-1.5 text-successText"><CheckCircle2 className="w-3.5 h-3.5" /> All changes saved</span>
            )}
            {state === "error" && (
                <span className="flex items-center gap-2 text-danger">
                    <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
                    <span className="max-w-[280px] truncate" title={error || undefined}>{error || "Save failed"}</span>
                    {onRetry && <button onClick={onRetry} className="underline hover:no-underline font-medium">Retry</button>}
                </span>
            )}
        </div>
    );
}
