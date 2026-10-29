// PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-5 AC-2 (F-4 #5 — CFF integrity banner).
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import {
    getUnmappedEntities,
    UnmappedEntity,
} from "@/lib/api/cff-integrity";
import { getApiErrorMessage } from "@/lib/api/errors";

/**
 * Callout banner for categorized-but-unmapped entities (F-4).
 *
 * When bank transactions have been categorized to a customer/vendor that has NO row in the
 * 13-week forecast, that money silently vanishes from the grid once the lookback window
 * rolls past it. This banner makes the state visible and offers a one-click "Add row" per
 * entity (the existing row-create path + a background snapshot refresh). NO silent
 * auto-create — the user owns the modeling decision.
 *
 * Dismissable-but-persistent: the X hides the banner for the current entity set only. It
 * returns on the next page visit, and immediately if NEW unmapped entities appear (the
 * dismissal is keyed to a signature of the current list, not a permanent flag).
 */
export function UnmappedEntitiesBanner({
    companyId,
    requestGeneration,
    refreshKey,
    onAddRow,
}: {
    companyId: string | null;
    requestGeneration: number;
    /** Bump to re-run detection (e.g. after an actuals review closes). */
    refreshKey: number;
    /** Page-owned, generation-registered add + forecast-adoption lifecycle. */
    onAddRow: (entity: UnmappedEntity) => Promise<void>;
}) {
    const ownerRef = useRef({ companyId, requestGeneration });
    ownerRef.current = { companyId, requestGeneration };
    const dismissalOwnerRef = useRef({ companyId, requestGeneration });
    const [loaded, setLoaded] = useState<{
        companyId: string | null;
        requestGeneration: number;
        entities: UnmappedEntity[];
    }>({ companyId: null, requestGeneration, entities: [] });
    const entities =
        loaded.companyId === companyId && loaded.requestGeneration === requestGeneration
            ? loaded.entities
            : [];
    const [dismissedSig, setDismissedSig] = useState<string | null>(null);
    const [addingKey, setAddingKey] = useState<string | null>(null);
    const [addError, setAddError] = useState<string | null>(null);

    useEffect(() => {
        if (
            dismissalOwnerRef.current.companyId !== companyId ||
            dismissalOwnerRef.current.requestGeneration !== requestGeneration
        ) {
            dismissalOwnerRef.current = { companyId, requestGeneration };
            setDismissedSig(null);
        }
        setAddingKey(null);
        setAddError(null);
        if (!companyId) {
            setLoaded({ companyId: null, requestGeneration, entities: [] });
            return;
        }
        const requestOwner = { companyId, requestGeneration };
        let cancelled = false;
        getUnmappedEntities(companyId)
            .then((resp) => {
                if (
                    !cancelled &&
                    ownerRef.current.companyId === requestOwner.companyId &&
                    ownerRef.current.requestGeneration === requestOwner.requestGeneration
                ) {
                    setLoaded({ ...requestOwner, entities: resp.unmapped || [] });
                }
            })
            .catch(() => {
                // Detection is advisory — a failed check must not break the forecast page.
                // Render nothing rather than a false "all clear" banner.
                if (
                    !cancelled &&
                    ownerRef.current.companyId === requestOwner.companyId &&
                    ownerRef.current.requestGeneration === requestOwner.requestGeneration
                ) {
                    setLoaded({ ...requestOwner, entities: [] });
                }
            });
        return () => {
            cancelled = true;
        };
    }, [companyId, refreshKey, requestGeneration]);

    const signature = entities
        .map((e) => `${e.entity_type}:${e.entity_name}`)
        .sort()
        .join("|");

    const handleAdd = useCallback(
        async (entity: UnmappedEntity) => {
            if (!companyId) return;
            const operationOwner = { companyId, requestGeneration };
            if (
                ownerRef.current.companyId !== operationOwner.companyId ||
                ownerRef.current.requestGeneration !== operationOwner.requestGeneration
            ) return;
            const key = `${entity.entity_type}:${entity.entity_name}`;
            setAddingKey(key);
            setAddError(null);
            try {
                await onAddRow(entity);
                if (
                    ownerRef.current.companyId !== operationOwner.companyId ||
                    ownerRef.current.requestGeneration !== operationOwner.requestGeneration
                ) return;
                setLoaded((previous) => ({
                    ...operationOwner,
                    entities:
                        previous.companyId === operationOwner.companyId &&
                        previous.requestGeneration === operationOwner.requestGeneration
                        ? previous.entities.filter(
                            (item) => !(
                                item.entity_name === entity.entity_name &&
                                item.entity_type === entity.entity_type
                            ),
                        )
                        : [],
                }));
            } catch (err) {
                if (
                    ownerRef.current.companyId !== operationOwner.companyId ||
                    ownerRef.current.requestGeneration !== operationOwner.requestGeneration
                ) return;
                setAddError(
                    getApiErrorMessage(err, `Couldn't add a forecast row for ${entity.entity_name}.`),
                );
            } finally {
                if (
                    ownerRef.current.companyId === operationOwner.companyId &&
                    ownerRef.current.requestGeneration === operationOwner.requestGeneration
                ) setAddingKey(null);
            }
        },
        [companyId, onAddRow, requestGeneration],
    );

    if (!companyId || entities.length === 0 || dismissedSig === signature) return null;

    const fmtAmount = (v: number) =>
        v.toLocaleString("en-US", {
            style: "currency",
            currency: "USD",
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        });

    return (
        <div
            role="alert"
            data-testid="cff-unmapped-banner"
            className="px-4 py-3 mb-4 flex-shrink-0 rounded-lg border border-warning/30 bg-warning/10 text-sm"
        >
            <div className="flex items-start justify-between gap-3">
                <div className="text-accentAmber">
                    <span className="font-semibold">
                        {entities.length === 1
                            ? "1 categorized entity isn't in this forecast."
                            : `${entities.length} categorized entities aren't in this forecast.`}
                    </span>{" "}
                    <span className="text-accentAmber/90">
                        Money categorized to them will not appear in the grid until each has a
                        forecast row — add one to keep their cash in the projection.
                    </span>
                </div>
                <button
                    onClick={() => setDismissedSig(signature)}
                    className="text-accentAmber/70 hover:text-accentAmber transition flex-shrink-0"
                    title="Dismiss for now (returns while these entities stay unmapped)"
                    aria-label="Dismiss unmapped entities callout"
                >
                    <X className="h-4 w-4" />
                </button>
            </div>
            <ul className="mt-2 space-y-1">
                {entities.map((entity) => {
                    const key = `${entity.entity_type}:${entity.entity_name}`;
                    return (
                        <li key={key} className="flex items-center gap-3 text-text">
                            <span className="font-medium truncate">{entity.entity_name}</span>
                            <span className="text-xs text-textMuted flex-shrink-0">
                                {entity.entity_type} · {entity.transaction_count}{" "}
                                {entity.transaction_count === 1 ? "transaction" : "transactions"} ·{" "}
                                {fmtAmount(entity.total_amount)}
                            </span>
                            <button
                                onClick={() => handleAdd(entity)}
                                disabled={addingKey !== null}
                                className="ml-auto flex-shrink-0 px-2 py-0.5 rounded border border-warning/40 text-xs font-medium text-accentAmber hover:bg-warning/20 transition disabled:opacity-50"
                            >
                                {addingKey === key ? "Adding…" : "Add row"}
                            </button>
                        </li>
                    );
                })}
            </ul>
            {addError && <div className="mt-2 text-xs text-danger">{addError}</div>}
        </div>
    );
}
