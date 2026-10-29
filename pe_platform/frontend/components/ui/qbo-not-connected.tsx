"use client";

import Link from "next/link";
import { Plug } from "lucide-react";
// PLAN-EXEMPT: QBO connection-health revocation-detection — the reconnect variant of the CTA.
import { getApiErrorMessage, isQboReconnectRequiredError } from "@/lib/api/errors";

/**
 * Empty-state CTA shown when the backend reports a QBO connection precondition (HTTP 409).
 * Two variants, driven by the error's error_code:
 *   - "qbo_not_connected"      → "Connect QuickBooks" (never-connected company)
 *   - "qbo_reconnect_required" → "Reconnect QuickBooks" (grant revoked on Intuit's side)
 * Use together with isQboConnectionRequiredError(error). Pass the error to surface the
 * backend's user_message. Both variants deep-link to /settings/qbo where the per-company
 * connect / reconnect buttons live.
 */
export function QboNotConnected({ error, className = "" }: { error?: unknown; className?: string }) {
    const reconnect = isQboReconnectRequiredError(error);
    const fallback = reconnect
        ? "QuickBooks disconnected this company on Intuit's side. Reconnect it to restore access."
        : "QuickBooks isn't connected for this company.";
    const message = error ? getApiErrorMessage(error, fallback) : fallback;
    const heading = reconnect ? "QuickBooks needs reconnecting" : "QuickBooks isn't connected";
    const cta = reconnect ? "Reconnect QuickBooks" : "Connect QuickBooks";
    return (
        <div className={`flex flex-col items-center justify-center text-center py-16 px-6 ${className}`}>
            <div className="flex items-center justify-center w-12 h-12 rounded-full bg-primary/10 text-primaryText mb-4">
                <Plug className="w-6 h-6" />
            </div>
            <p className="font-medium text-text">{heading}</p>
            <p className="text-sm text-textMuted mt-1 max-w-md">{message}</p>
            {/* TSX-OK: button-component cannot express this call site — Button.asChild is an unwired prop (no Slot composition); kept as a styled Link. */}
            <Link
                href="/settings/qbo"
                className="mt-4 inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-white text-sm font-medium hover:bg-primary/90 transition"
            >
                <Plug className="w-4 h-4" /> {cta}
            </Link>
        </div>
    );
}
