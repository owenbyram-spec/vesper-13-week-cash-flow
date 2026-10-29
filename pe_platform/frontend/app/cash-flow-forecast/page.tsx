"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { Download, Inbox, Loader2, RefreshCw, X } from "lucide-react";
import { useAppStore } from "@/lib/store";
import { apiClient } from "@/lib/api/client";
import { SaveStatus } from "@/components/ui/modals";
import { QboNotConnected } from "@/components/ui/qbo-not-connected";
// PLAN-EXEMPT: QBO connection-health — gate the CTA on EITHER precondition (never-connected OR revoked-reconnect).
import { getApiErrorMessage, isQboConnectionRequiredError as isQboNotConnectedError } from "@/lib/api/errors";
// PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-5 (F-4 — CFF integrity wiring).
import { addForecastRow, type UnmappedEntity } from "@/lib/api/cff-integrity";
import { UnmappedEntitiesBanner } from "./unmapped-entities-banner";
import {
    downloadCashflowForecastExport,
    type CashflowForecastExportFormat,
} from "@/lib/api/cashflow-forecast-export";
import { Button } from "@/components/ui/button";

function weekStatusLabel(status?: WeekStatus["status"]) {
    switch (status) {
        case "not_pulled": return "Needs pull";
        case "pending_categorization": return "Pending";
        case "pulled": return "Pulled";
        case "closed": return "Closed";
        case "reconciled": return "Reconciled";
        default: return "";
    }
}

/* ── helpers ───────────────────────────────────────────────────────────── */

// P-2 (AUDIT.md RC-5 / punch-list #8): these previously collapsed a genuine $0 week
// (no cash movement — a real, common CFF outcome) into the SAME "—" as missing/null —
// zero is data, not absence. Only null/undefined render the no-data dash now.
function fmt(v: number | null | undefined) {
    if (v == null) return "—";
    const abs = Math.abs(v);
    const str = `$${abs.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    return v < 0 ? `(${str})` : str;
}
function roundCurrency(v: number) {
    return Math.round(v * 100) / 100;
}

function fmtDate(value?: string | null) {
    if (!value) return "";
    const dateValue = /^\d{4}-\d{2}-\d{2}$/.test(value)
        ? new Date(value + "T00:00:00")
        : new Date(value);
    if (Number.isNaN(dateValue.getTime())) return value;
    return dateValue.toLocaleDateString("en-US", {
        month: "short",
        day: "numeric",
        year: "numeric",
    });
}

function anchorSummaryMismatch(diagnostics?: AnchorDiagnostics | null) {
    const summary = diagnostics?.bank_accounts_summary_amount;
    const rowTotal = diagnostics?.bank_accounts_row_total;
    return summary != null && rowTotal != null && Math.abs(summary - rowTotal) >= 0.01;
}

// Delegates to the shared resolver so structured `detail` objects never reach
// the render path. See lib/api/errors.ts.
function getErrorMessage(error: unknown, fallback: string) {
    return getApiErrorMessage(error, fallback);
}

/* ── types ─────────────────────────────────────────────────────────────── */

interface EntityRow {
    row_id?: string;
    entity_key?: string | number;
    entity_id?: string | number;
    qbo_id?: string | number;
    id?: string | number;
    row_key: string;
    name: string;
    day_of_month: number | null;
    effective_day: number | null;
    avg_amount: number | null;
    effective_avg: number | null;
    amounts: Array<number | null>;
    sort_order: number;
    source?: "invoice" | "pattern" | "hybrid" | "manual" | "actual";
    open_invoices?: number;
    total_open_balance?: number | null;
    known_amounts?: Array<number | null>;
    run_rate_amounts?: Array<number | null>;
    run_rate_source?: string;
    past_due_balance?: number | null;
    dso_days?: number;
    has_historical_data?: boolean;
}

interface WeekStatus {
    week_ending: string;
    status: "not_pulled" | "pending_categorization" | "pulled" | "closed" | "reconciled";
    transaction_count: number;
    categorized_count: number;
    uncategorized_count: number;
    excluded_count: number;
    cascade_ready: boolean;
    pulled_at?: string | null;
    reviewed_at?: string | null;
    reconciled_at?: string | null;
}

interface AnchorDiagnostics {
    requested_end_date?: string | null;
    qbo_header_end_period?: string | null;
    qbo_header_report_basis?: string | null;
    amount_column_title?: string | null;
    bank_accounts_summary_amount?: number | null;
    bank_accounts_row_total?: number | null;
    bank_accounts_row_count?: number | null;
    cash_account_universe?: CashAccountUniverseDiagnostics | null;
}

interface CashAccountUniverseDiagnostics {
    balance_sheet_as_of?: string | null;
    included_count?: number | null;
    clearing_count?: number | null;
    unmatched_balance_sheet_row_count?: number | null;
    warnings?: string[];
}

interface ForecastData {
    week_headers: string[];
    lookback: number;
    forecast_reanchored_to_control?: boolean;
    beginning_balances: Array<number | null>;
    opening_balance_week_ending?: string | null;
    first_forecast_week_ending?: string | null;
    customers: EntityRow[];
    other_cash_in: Array<number | null>;
    total_cash_in: Array<number | null>;
    vendors: EntityRow[];
    other_cash_out: Array<number | null>;
    total_cash_out: Array<number | null>;
    ending_balances: Array<number | null>;
    bank_balance: number | null;
    bank_balance_as_of?: string | null;
    bank_balance_source?: string | null;
    anchor_diagnostics?: AnchorDiagnostics | null;
    reconciliation_delta?: {
        anchor_as_of: string;
        prior_as_of: string;
        anchor_bank_balance: number;
        prior_bank_balance: number;
        balance_sheet_change: number;
        actuals_net_change: number;
        delta: number;
        prior_balance_source?: string | null;
        anchor_balance_source?: string | null;
    } | null;
    beginning_balance_override: number | null;
    uncategorized_actuals?: number;
    week_statuses?: WeekStatus[];
    review_required_weeks?: string[];
    // 2A: surfaced from the backend forecast envelope — true when a QBO section
    // failed to load, so the grid is built on partial data (not a clean zero).
    data_quality?: {
        is_partial: boolean;
        unavailable_sections: string[];
    };
    // 2026-07-09 honest-failure batch (AC-C2/C3/C5): the backend's honest-gap ledger.
    // `unmatched` counts categorized items the forecast could NOT place (with reasons);
    // `beyond_horizon` is open cash expected past the last grid week (excluded from the
    // grid, reported here). beyond_horizon === null means the serving snapshot predates
    // the accounting (unknown), not zero.
    unmatched?: {
        count: number;
        reasons: Record<string, { count: number; total_amount: number }>;
        entities: unknown[];
    };
    beyond_horizon?: {
        cash_in: number;
        cash_out: number;
        item_count: number;
        policy?: string;
    } | null;
    // History payees the backend did NOT project as a recurring run-rate (one-off, sparse,
    // irregular or unattributed history). null = the snapshot predates this accounting.
    non_recurring_history?: NonRecurringPayee[] | null;
}

interface NonRecurringPayee {
    name: string;
    entity_type: "customer" | "vendor";
    occurrences: number;
    distinct_months: number;
    total_amount: number;
    last_date: string;
    reason: string;
}

interface EditingCell {
    row: string;        // stable row identity or a built-in row sentinel
    entityName: string; // server mutation name; equals row for built-in sentinels
    type: "customer" | "vendor" | "balance";
    col: number;        // -2 = editing day, -1 = editing avg, 0+ = weekly cell
    originalValue: number | null;
}

type EditTarget = Omit<EditingCell, "originalValue"> & { value: number | null };

interface SpreadFormState {
    entity_name: string;
    entity_type: "customer" | "vendor";
    total_amount: string;
    periods: string;
    cadence: "weekly" | "monthly";
    start_week_ending: string;
    day_of_month: string;
    note: string;
}

interface ActualTransaction {
    id: number;
    qbo_txn_id: string;
    qbo_txn_type: string;
    qbo_account_id?: string | null;
    txn_date: string;
    amount: number;
    direction: "in" | "out";
    description?: string | null;
    account_name?: string | null;
    entity_name?: string | null;
    entity_type?: string | null;
    category: string;
    is_billcom?: boolean | null;
}

interface EntityOption {
    id: string;
    name: string;
    type?: string;
}

interface ForecastExportErrorDetail {
    code?: string;
    error_code?: string;
    message?: string;
    user_message?: string;
    retryable?: boolean;
}

interface ForecastResponse extends ForecastData {
    snapshot_age_seconds?: number | null;
    snapshot_state?: string;
    export_id: string | null;
    export_company_id: string;
    export_schema_version: number;
    export_capture_state: "ready" | "building" | "unavailable";
    export_error: ForecastExportErrorDetail | string | null;
}

function normalizeEntityRows(
    rows: EntityRow[],
    type: "customer" | "vendor",
) {
    const occurrences = new Map<string, number>();
    return rows.map((row) => {
        const occurrence = occurrences.get(row.name) ?? 0;
        occurrences.set(row.name, occurrence + 1);
        const explicitEntityKey = row.entity_key
            ?? row.entity_id
            ?? row.qbo_id
            ?? row.id;
        const resolvedEntityKey = explicitEntityKey != null && String(explicitEntityKey).length > 0
            ? String(explicitEntityKey)
            : `name:${encodeURIComponent(row.name)}:occurrence:${occurrence}`;
        const rowKey = row.row_id && row.row_id.length > 0
            ? row.row_id
            : `${type}:${resolvedEntityKey}`;
        return {
            ...row,
            row_key: rowKey,
        };
    });
}

function normalizeForecastResponse(response: ForecastResponse): ForecastResponse {
    return {
        ...response,
        customers: normalizeEntityRows(response.customers, "customer"),
        vendors: normalizeEntityRows(response.vendors, "vendor"),
    };
}

interface OwnerToken {
    companyId: string | null;
    requestGeneration: number;
}

interface VisibleForecastState {
    companyId: string;
    requestGeneration: number;
    mutationEpoch: number;
    exportId: string | null;
    exportSchemaVersion: number | null;
    exportCaptureState: ForecastResponse["export_capture_state"];
    exportError: ForecastResponse["export_error"];
    forecast: ForecastData;
    snapshotState: string;
    snapshotAge: number | null;
}

interface OperationCoordinator {
    companyId: string | null;
    requestGeneration: number;
    mutationEpoch: number;
    activeMutationEpochs: Set<number>;
    pendingMutations: Set<Promise<unknown>>;
    needsAdoption: boolean;
    latestFailedMutationEpoch: Map<string, number>;
    latestSucceededMutationEpoch: Map<string, number>;
    latestAdmittedMutationEpoch: Map<string, number>;
    mutationAdmissionOpen: boolean;
    exportLocked: boolean;
    exportPromise: Promise<void> | null;
    refreshPromise: Promise<void> | null;
    initialLoadDone: boolean;
    initialLoadStarted: boolean;
}

function createCoordinator(owner: OwnerToken): OperationCoordinator {
    return {
        companyId: owner.companyId,
        requestGeneration: owner.requestGeneration,
        mutationEpoch: 0,
        activeMutationEpochs: new Set(),
        pendingMutations: new Set(),
        needsAdoption: false,
        latestFailedMutationEpoch: new Map(),
        latestSucceededMutationEpoch: new Map(),
        latestAdmittedMutationEpoch: new Map(),
        mutationAdmissionOpen: true,
        exportLocked: false,
        exportPromise: null,
        refreshPromise: null,
        initialLoadDone: false,
        initialLoadStarted: false,
    };
}

function hasUncorrectedMutationFailure(
    coordinator: OperationCoordinator,
    adopting?: { key: string; epoch: number },
) {
    for (const [key, failedEpoch] of coordinator.latestFailedMutationEpoch) {
        const settledSuccessEpoch = coordinator.latestSucceededMutationEpoch.get(key) ?? -1;
        const adoptingSuccessEpoch = adopting?.key === key ? adopting.epoch : -1;
        if (Math.max(settledSuccessEpoch, adoptingSuccessEpoch) <= failedEpoch) return true;
    }
    return false;
}

function isLatestMutationForKey(
    coordinator: OperationCoordinator | null,
    operationKey: string,
    mutationEpoch: number,
) {
    return coordinator?.latestAdmittedMutationEpoch.get(operationKey) === mutationEpoch;
}

function sameOwnerToken(left: OwnerToken, right: OwnerToken) {
    return left.companyId === right.companyId && left.requestGeneration === right.requestGeneration;
}

class StaleForecastOperationError extends Error {
    constructor() {
        super("Forecast operation owner changed before it started.");
        this.name = "StaleForecastOperationError";
    }
}

function exportErrorMessage(detail: ForecastResponse["export_error"]) {
    if (typeof detail === "string") return detail;
    return detail?.user_message || detail?.message || "This forecast is not ready to download yet.";
}

/* ── component ─────────────────────────────────────────────────────────── */

export default function CashFlowForecastPage() {
    // Subscribe ONLY to activeCompanyId to avoid re-renders from unrelated store changes
    const companyId = useAppStore((s) => s.activeCompanyId);
    const ownerRef = useRef<OwnerToken>({ companyId, requestGeneration: 0 });
    if (ownerRef.current.companyId !== companyId) {
        ownerRef.current = {
            companyId,
            requestGeneration: ownerRef.current.requestGeneration + 1,
        };
    }
    const renderOwner = ownerRef.current;
    const coordinatorRef = useRef<OperationCoordinator>(createCoordinator(renderOwner));
    if (
        coordinatorRef.current.companyId !== renderOwner.companyId ||
        coordinatorRef.current.requestGeneration !== renderOwner.requestGeneration
    ) {
        coordinatorRef.current = createCoordinator(renderOwner);
    }

    const [ownedForecastState, setOwnedForecastState] = useState<VisibleForecastState | null>(null);
    const ownedForecastStateRef = useRef<VisibleForecastState | null>(null);
    ownedForecastStateRef.current = ownedForecastState;
    const [uiOwner, setUiOwner] = useState<OwnerToken>(renderOwner);
    const uiIsCurrent =
        uiOwner.companyId === companyId &&
        uiOwner.requestGeneration === renderOwner.requestGeneration;
    const visibleState =
        ownedForecastState?.companyId === companyId &&
        ownedForecastState.requestGeneration === renderOwner.requestGeneration
            ? ownedForecastState
            : null;
    const data = visibleState?.forecast ?? null;
    const snapshotAge = visibleState?.snapshotAge ?? null;

    const [loadingState, setLoading] = useState(true);
    const [refreshingState, setRefreshing] = useState(false);
    const [errorState, setError] = useState<string | null>(null);
    // Raw thrown value from the most recent LOAD/REFRESH, kept so we can detect
    // the qbo_not_connected precondition (which needs the axios error shape, not
    // the resolved string in `error`). Cleared on every load attempt.
    const [loadErrorRawState, setLoadErrorRaw] = useState<unknown>(null);
    const loading = uiIsCurrent ? loadingState : Boolean(companyId);
    const refreshing = uiIsCurrent ? refreshingState : false;
    const error = uiIsCurrent ? errorState : null;
    const loadErrorRaw = uiIsCurrent ? loadErrorRawState : null;

    // Inline editing
    const [editing, setEditing] = useState<EditingCell | null>(null);
    const [editValue, setEditValue] = useState("");
    const [savingState, setSaving] = useState(false);
    const [savedFlashState, setSavedFlash] = useState(false);
    const saving = uiIsCurrent ? savingState : false;
    const savedFlash = uiIsCurrent ? savedFlashState : false;
    const savedFlashTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const inputRef = useRef<HTMLInputElement>(null);
    const editingRef = useRef<EditingCell | null>(null);
    const editValueRef = useRef("");
    const savingCountRef = useRef(0);
    const committingKeysRef = useRef<Set<string>>(new Set());
    const commitPromisesRef = useRef<Map<string, Promise<void>>>(new Map());
    const skipNextBlurRef = useRef(false);
    const skipBlurTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    // Add new entity
    const [addingEntity, setAddingEntity] = useState<"customer" | "vendor" | null>(null);
    const [newEntityName, setNewEntityName] = useState("");
    const addEntityRef = useRef<HTMLInputElement>(null);

    // Drag-and-drop reorder
    const [dragType, setDragType] = useState<"customer" | "vendor" | null>(null);
    const [dragIdx, setDragIdx] = useState<number | null>(null);
    const [dragOverIdx, setDragOverIdx] = useState<number | null>(null);

    // Sidebar collapsed state
    const [sidebarOpen, setSidebarOpen] = useState(true);
    const [showSpreadModal, setShowSpreadModal] = useState(false);
    const [spreadSaving, setSpreadSaving] = useState(false);
    const [spreadError, setSpreadError] = useState<string | null>(null);
    const [spreadForm, setSpreadForm] = useState<SpreadFormState>({
        entity_name: "",
        entity_type: "vendor",
        total_amount: "",
        periods: "6",
        cadence: "weekly",
        start_week_ending: "",
        day_of_month: "15",
        note: "",
    });

    // Actuals categorization
    const [showCategorize, setShowCategorize] = useState(false);
    const [actualsLoading, setActualsLoading] = useState(false);
    const [actuals, setActuals] = useState<ActualTransaction[]>([]);
    const [actualsWeek, setActualsWeek] = useState<string>("");
    const [actualsCashUniverse, setActualsCashUniverse] = useState<CashAccountUniverseDiagnostics | null>(null);
    const [entityList, setEntityList] = useState<{ customers: EntityOption[]; vendors: EntityOption[] }>({ customers: [], vendors: [] });
    const [activeDropdown, setActiveDropdown] = useState<number | null>(null);
    const [dropdownSearch, setDropdownSearch] = useState("");
    const dropdownSearchRef = useRef<string>("");
    const dropdownDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    // PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-5 (F-4 — CFF integrity state).
    // Bump to re-run unmapped-entity detection (banner), and the categorize-time warning
    // for an entity with no forecast row (the additive cff_unmapped flag).
    const [integrityRefreshKey, setIntegrityRefreshKey] = useState(0);
    const [unmappedWarning, setUnmappedWarning] = useState<{ name: string; type: "customer" | "vendor" } | null>(null);
    const [warningAddBusy, setWarningAddBusy] = useState(false);
    const [exportBusyFormatState, setExportBusyFormat] = useState<CashflowForecastExportFormat | null>(null);
    const [exportErrorState, setExportError] = useState<string | null>(null);
    const exportBusyFormat = uiIsCurrent ? exportBusyFormatState : null;
    const exportError = uiIsCurrent ? exportErrorState : null;

    const isCurrentOwner = useCallback((token: OwnerToken) => (
        ownerRef.current.companyId === token.companyId &&
        ownerRef.current.requestGeneration === token.requestGeneration
    ), []);

    const coordinatorFor = useCallback((token: OwnerToken) => {
        const coordinator = coordinatorRef.current;
        if (
            !isCurrentOwner(token) ||
            coordinator.companyId !== token.companyId ||
            coordinator.requestGeneration !== token.requestGeneration
        ) return null;
        return coordinator;
    }, [isCurrentOwner]);

    const storeVisibleState = useCallback((token: OwnerToken, next: VisibleForecastState) => {
        if (!isCurrentOwner(token)) return false;
        ownedForecastStateRef.current = next;
        setOwnedForecastState(next);
        return true;
    }, [isCurrentOwner]);

    const adoptForecastResponse = useCallback((
        token: OwnerToken,
        response: ForecastResponse,
        options: {
            expectedMutationEpoch: number;
            clearMutationState: boolean;
            adoptingMutationEpoch?: number;
            adoptingMutationKey?: string;
        },
    ) => {
        const coordinator = coordinatorFor(token);
        if (!coordinator || !token.companyId) return false;
        if (coordinator.mutationEpoch !== options.expectedMutationEpoch) return false;
        if (response.export_company_id !== token.companyId) {
            throw new Error("The forecast response did not match the selected company.");
        }
        const normalizedResponse = normalizeForecastResponse(response);

        const hasOtherActiveMutation = [...coordinator.activeMutationEpochs].some(
            (epoch) => epoch !== options.adoptingMutationEpoch,
        );
        const adopting = options.adoptingMutationKey && options.adoptingMutationEpoch != null
            ? { key: options.adoptingMutationKey, epoch: options.adoptingMutationEpoch }
            : undefined;
        const hasUncorrectedFailure = hasUncorrectedMutationFailure(coordinator, adopting);
        const clearsMutationState =
            options.clearMutationState &&
            !hasOtherActiveMutation &&
            !hasUncorrectedFailure;
        if (clearsMutationState) {
            coordinator.needsAdoption = false;
            coordinator.latestFailedMutationEpoch.clear();
            coordinator.latestSucceededMutationEpoch.clear();
        }

        const exportIsReady =
            !coordinator.needsAdoption &&
            !hasUncorrectedMutationFailure(coordinator, adopting) &&
            response.export_capture_state === "ready" &&
            typeof response.export_id === "string" &&
            response.export_id.length > 0;
        const next: VisibleForecastState = {
            companyId: token.companyId,
            requestGeneration: token.requestGeneration,
            mutationEpoch: coordinator.mutationEpoch,
            exportId: exportIsReady ? response.export_id : null,
            exportSchemaVersion: Number.isInteger(response.export_schema_version)
                ? response.export_schema_version
                : null,
            exportCaptureState: response.export_capture_state || "unavailable",
            exportError: response.export_error ?? null,
            forecast: normalizedResponse,
            snapshotState: normalizedResponse.snapshot_state || "ready",
            snapshotAge: normalizedResponse.snapshot_age_seconds ?? null,
        };
        return storeVisibleState(token, next);
    }, [coordinatorFor, storeVisibleState]);

    const fetchAndAdoptForecast = useCallback(async (
        token: OwnerToken,
        options: {
            expectedMutationEpoch: number;
            clearMutationState: boolean;
            adoptingMutationEpoch?: number;
            adoptingMutationKey?: string;
            initial?: boolean;
        },
    ) => {
        const coordinator = coordinatorFor(token);
        if (!coordinator || !token.companyId) return false;
        if (options.initial && !coordinator.initialLoadDone) setLoading(true);
        setError(null);
        setLoadErrorRaw(null);
        try {
            const result = await apiClient.get("/cashflow-forecast/weekly", {
                params: { company_id: token.companyId, weeks: 13, lookback: 1 },
            });
            if (!isCurrentOwner(token)) return false;
            const adopted = adoptForecastResponse(token, result.data as ForecastResponse, options);
            if (adopted) coordinator.initialLoadDone = true;
            return adopted;
        } catch (caught: unknown) {
            if (isCurrentOwner(token)) {
                setLoadErrorRaw(caught);
                setError(getErrorMessage(caught, "Failed to fetch"));
            }
            throw caught;
        } finally {
            if (isCurrentOwner(token)) setLoading(false);
        }
    }, [adoptForecastResponse, coordinatorFor, isCurrentOwner]);

    const registerMutation = useCallback(<T,>(
        token: OwnerToken,
        work: (mutationEpoch: number) => Promise<T>,
        options: { operationKey: string; allowDuringExport?: boolean },
    ): Promise<T> | null => {
        const coordinator = coordinatorFor(token);
        if (!coordinator || !token.companyId) return null;
        if (!coordinator.mutationAdmissionOpen && !options.allowDuringExport) {
            setError("A forecast download is finalizing. Retry this change after it completes.");
            return null;
        }

        const mutationEpoch = coordinator.mutationEpoch + 1;
        coordinator.mutationEpoch = mutationEpoch;
        coordinator.latestAdmittedMutationEpoch.set(options.operationKey, mutationEpoch);
        coordinator.needsAdoption = true;
        coordinator.activeMutationEpochs.add(mutationEpoch);

        const current = ownedForecastStateRef.current;
        if (
            current?.companyId === token.companyId &&
            current.requestGeneration === token.requestGeneration
        ) {
            storeVisibleState(token, {
                ...current,
                mutationEpoch,
                exportId: null,
            });
        }

        const started = Promise.resolve().then(() => {
            if (!isCurrentOwner(token)) throw new StaleForecastOperationError();
            return work(mutationEpoch);
        });
        const completed = started.then((value) => {
            const priorSuccess = coordinator.latestSucceededMutationEpoch.get(options.operationKey) ?? -1;
            if (mutationEpoch > priorSuccess) {
                coordinator.latestSucceededMutationEpoch.set(options.operationKey, mutationEpoch);
            }
            return value;
        });
        const tracked = completed
            .catch((caught: unknown) => {
                if (!(caught instanceof StaleForecastOperationError)) {
                    const priorFailure = coordinator.latestFailedMutationEpoch.get(options.operationKey) ?? -1;
                    if (mutationEpoch > priorFailure) {
                        coordinator.latestFailedMutationEpoch.set(options.operationKey, mutationEpoch);
                    }
                }
                throw caught;
            })
            .finally(() => {
                coordinator.activeMutationEpochs.delete(mutationEpoch);
                coordinator.pendingMutations.delete(tracked);
            });
        coordinator.pendingMutations.add(tracked);
        return tracked;
    }, [coordinatorFor, isCurrentOwner, storeVisibleState]);

    const refreshFromQBO = useCallback(() => {
        const token = ownerRef.current;
        if (!sameOwnerToken(uiOwner, token)) return Promise.resolve();
        const coordinator = coordinatorFor(token);
        if (!coordinator || !token.companyId) return Promise.resolve();
        if (coordinator.refreshPromise) return coordinator.refreshPromise;
        if (!coordinator.mutationAdmissionOpen) {
            setError("A forecast download is finalizing. Refresh after it completes.");
            return Promise.resolve();
        }

        const startingMutationEpoch = coordinator.mutationEpoch;
        setRefreshing(true);
        setError(null);
        setLoadErrorRaw(null);
        const refreshTask = Promise.resolve()
            .then(async () => {
                if (!isCurrentOwner(token)) return;
                const result = await apiClient.post("/cashflow-forecast/weekly/refresh", null, {
                    params: { company_id: token.companyId, weeks: 13, lookback: 1 },
                });
                if (!isCurrentOwner(token) || coordinator.mutationEpoch !== startingMutationEpoch) return;
                adoptForecastResponse(token, result.data as ForecastResponse, {
                    expectedMutationEpoch: startingMutationEpoch,
                    clearMutationState: false,
                });
            })
            .catch((caught: unknown) => {
                if (isCurrentOwner(token)) {
                    setLoadErrorRaw(caught);
                    setError(getErrorMessage(caught, "Refresh failed"));
                }
            })
            .finally(() => {
                if (coordinator.refreshPromise === refreshTask) coordinator.refreshPromise = null;
                if (isCurrentOwner(token)) setRefreshing(false);
            });
        coordinator.refreshPromise = refreshTask;
        return refreshTask;
    }, [adoptForecastResponse, coordinatorFor, isCurrentOwner, uiOwner]);

    useEffect(() => {
        const token: OwnerToken = {
            companyId,
            requestGeneration: renderOwner.requestGeneration,
        };
        const coordinator = coordinatorFor(token);
        ownedForecastStateRef.current = null;
        setOwnedForecastState(null);
        setUiOwner(token);
        setLoading(Boolean(companyId));
        setRefreshing(false);
        setError(null);
        setLoadErrorRaw(null);
        setEditing(null);
        editingRef.current = null;
        setEditValue("");
        editValueRef.current = "";
        setSaving(false);
        setSavedFlash(false);
        savingCountRef.current = 0;
        committingKeysRef.current.clear();
        commitPromisesRef.current.clear();
        skipNextBlurRef.current = false;
        if (skipBlurTimerRef.current) {
            clearTimeout(skipBlurTimerRef.current);
            skipBlurTimerRef.current = null;
        }
        setAddingEntity(null);
        setNewEntityName("");
        setDragType(null);
        setDragIdx(null);
        setDragOverIdx(null);
        setShowSpreadModal(false);
        setSpreadSaving(false);
        setSpreadError(null);
        setSpreadForm({
            entity_name: "",
            entity_type: "vendor",
            total_amount: "",
            periods: "6",
            cadence: "weekly",
            start_week_ending: "",
            day_of_month: "15",
            note: "",
        });
        setShowCategorize(false);
        setActualsLoading(false);
        setActuals([]);
        setActualsWeek("");
        setActualsCashUniverse(null);
        setEntityList({ customers: [], vendors: [] });
        setActiveDropdown(null);
        setDropdownSearch("");
        dropdownSearchRef.current = "";
        setIntegrityRefreshKey(0);
        setUnmappedWarning(null);
        setWarningAddBusy(false);
        setExportBusyFormat(null);
        setExportError(null);
        if (savedFlashTimerRef.current) {
            clearTimeout(savedFlashTimerRef.current);
            savedFlashTimerRef.current = null;
        }
        if (dropdownDebounceRef.current) {
            clearTimeout(dropdownDebounceRef.current);
            dropdownDebounceRef.current = null;
        }

        if (token.companyId && coordinator && !coordinator.initialLoadStarted) {
            coordinator.initialLoadStarted = true;
            void fetchAndAdoptForecast(token, {
                expectedMutationEpoch: coordinator.mutationEpoch,
                clearMutationState: true,
                initial: true,
            }).catch(() => {
                if (isCurrentOwner(token)) coordinator.initialLoadStarted = false;
            });
        } else if (!token.companyId) {
            setLoading(false);
        }

        return () => {
            if (savedFlashTimerRef.current) {
                clearTimeout(savedFlashTimerRef.current);
                savedFlashTimerRef.current = null;
            }
            if (dropdownDebounceRef.current) {
                clearTimeout(dropdownDebounceRef.current);
                dropdownDebounceRef.current = null;
            }
            if (skipBlurTimerRef.current) {
                clearTimeout(skipBlurTimerRef.current);
                skipBlurTimerRef.current = null;
            }
        };
    }, [companyId, coordinatorFor, fetchAndAdoptForecast, isCurrentOwner, renderOwner.requestGeneration]);

    const statusByWeek = useMemo(() => {
        const entries = data?.week_statuses || [];
        return new Map(entries.map((item) => [item.week_ending, item]));
    }, [data?.week_statuses]);

    const reviewRequiredWeeks = data?.review_required_weeks || [];

    const pullActualsData = useCallback(async (token: OwnerToken, weekEnding?: string) => {
        if (!token.companyId) return false;
        const params: Record<string, string> = { company_id: token.companyId };
        if (weekEnding) params.week_ending = weekEnding;
        const [actualsResult, entitiesResult] = await Promise.all([
            apiClient.get("/cashflow-forecast/actuals/pull", { params }),
            apiClient.get("/cashflow-forecast/entities", {
                params: { company_id: token.companyId },
            }),
        ]);
        if (!isCurrentOwner(token)) return false;
        setActuals(actualsResult.data.transactions || []);
        setActualsWeek(actualsResult.data.week_ending || weekEnding || "");
        setActualsCashUniverse(actualsResult.data.cash_account_universe || null);
        setEntityList(entitiesResult.data || { customers: [], vendors: [] });
        return true;
    }, [isCurrentOwner]);

    const openActualsReview = useCallback((weekEnding?: string) => {
        const token = ownerRef.current;
        if (!sameOwnerToken(uiOwner, token)) return Promise.resolve();
        const operation = registerMutation(token, async () => {
            if (!isCurrentOwner(token)) return;
            setShowCategorize(true);
            setActualsLoading(true);
            setActualsCashUniverse(null);
            setError(null);
            setUnmappedWarning(null);
            try {
                await pullActualsData(token, weekEnding);
            } catch (caught: unknown) {
                if (isCurrentOwner(token)) {
                    setError(getErrorMessage(caught, "Failed to pull actuals"));
                }
                throw caught;
            } finally {
                if (isCurrentOwner(token)) setActualsLoading(false);
            }
        }, { operationKey: `actuals-pull:${weekEnding || "latest"}` });
        return operation?.catch(() => undefined) ?? Promise.resolve();
    }, [isCurrentOwner, pullActualsData, registerMutation, uiOwner]);

    const finishActualsReview = useCallback(() => {
        const token = ownerRef.current;
        if (!sameOwnerToken(uiOwner, token)) return Promise.resolve();
        const weekEnding = actualsWeek;
        const hasPending = actuals.some((txn) => txn.category === "uncategorized");
        const mutationKey = `actuals-close:${weekEnding || "latest"}`;
        const operation = registerMutation(token, async (mutationEpoch) => {
            if (token.companyId && weekEnding && !hasPending) {
                await apiClient.post("/cashflow-forecast/actuals/close", null, {
                    params: { company_id: token.companyId, week_ending: weekEnding },
                });
            }
            if (!isCurrentOwner(token)) return;
            await fetchAndAdoptForecast(token, {
                expectedMutationEpoch: mutationEpoch,
                clearMutationState: true,
                adoptingMutationEpoch: mutationEpoch,
                adoptingMutationKey: mutationKey,
            });
            if (!isCurrentOwner(token)) return;
            setShowCategorize(false);
            setUnmappedWarning(null);
            setIntegrityRefreshKey((key) => key + 1);
        }, { operationKey: mutationKey });
        if (!operation) return Promise.resolve();
        return operation.catch((caught: unknown) => {
            if (isCurrentOwner(token)) {
                setError(getErrorMessage(caught, "Could not close actuals review"));
            }
        });
    }, [actuals, actualsWeek, fetchAndAdoptForecast, isCurrentOwner, registerMutation, uiOwner]);

    // PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-5 AC-3 (F-4 — one-click add from warning).
    const handleWarningAddRow = useCallback(() => {
        const token = ownerRef.current;
        if (!sameOwnerToken(uiOwner, token)) return Promise.resolve();
        const warning = unmappedWarning;
        if (!warning) return Promise.resolve();
        const mutationKey = `add-row:${warning.type}:${warning.name}`;
        const operation = registerMutation(token, async (mutationEpoch) => {
            if (!token.companyId || !isCurrentOwner(token)) return;
            setWarningAddBusy(true);
            try {
                await addForecastRow(token.companyId, warning.name, warning.type);
                if (!isCurrentOwner(token)) return;
                await fetchAndAdoptForecast(token, {
                    expectedMutationEpoch: mutationEpoch,
                    clearMutationState: true,
                    adoptingMutationEpoch: mutationEpoch,
                    adoptingMutationKey: mutationKey,
                });
                if (!isCurrentOwner(token)) return;
                setUnmappedWarning(null);
                setIntegrityRefreshKey((key) => key + 1);
            } finally {
                if (isCurrentOwner(token)) setWarningAddBusy(false);
            }
        }, { operationKey: mutationKey });
        if (!operation) return Promise.resolve();
        return operation.catch((caught: unknown) => {
            if (isCurrentOwner(token)) {
                setError(getErrorMessage(caught, "Couldn't add the forecast row. Please try again."));
            }
        });
    }, [fetchAndAdoptForecast, isCurrentOwner, registerMutation, uiOwner, unmappedWarning]);

    useEffect(() => {
        if (!data?.week_headers?.length) return;
        setSpreadForm((prev) => (
            prev.start_week_ending
                ? prev
                : { ...prev, start_week_ending: data.week_headers[0] }
        ));
    }, [data?.week_headers]);

    // Focus input when editing starts
    useEffect(() => {
        if (editing && inputRef.current) {
            inputRef.current.focus();
            inputRef.current.select();
        }
    }, [editing]);

    useEffect(() => {
        editingRef.current = editing;
    }, [editing]);

    useEffect(() => {
        editValueRef.current = editValue;
    }, [editValue]);

    /* ── cell editing ─────────────────────────────────────────────────── */

    const formatEditValue = (col: number, value: number | null) => (
        value == null || value === 0 ? "" : col === -2 ? value.toFixed(0) : value.toFixed(2)
    );

    const updateEditValue = (value: string) => {
        editValueRef.current = value;
        setEditValue(value);
    };

    const openEditTarget = (target: EditTarget) => {
        const next = {
            row: target.row,
            entityName: target.entityName,
            type: target.type,
            col: target.col,
            originalValue: target.value,
        };
        editingRef.current = next;
        setEditing(next);
        updateEditValue(formatEditValue(target.col, target.value));
    };

    const startEdit = (
        row: string,
        type: EditingCell["type"],
        col: number,
        currentValue: number | null,
        entityName: string = row,
    ) => {
        openEditTarget({ row, entityName, type, col, value: currentValue });
    };

    const cancelEdit = () => {
        editingRef.current = null;
        setEditing(null);
    };

    const buildEditTargets = useCallback((): EditTarget[] => {
        if (!data) return [];
        const targets: EditTarget[] = [];
        const weekCount = data.week_headers.length;
        if ((data.lookback ?? 0) === 0 && data.beginning_balances.length > 0) {
            targets.push({
                row: "__beginning_balance",
                entityName: "__beginning_balance",
                type: "balance",
                col: 0,
                value: data.beginning_balances[0] ?? null,
            });
        }
        data.customers.forEach((customer) => {
            targets.push({ row: customer.row_key, entityName: customer.name, type: "customer", col: -2, value: customer.effective_day ?? customer.day_of_month ?? null });
            targets.push({ row: customer.row_key, entityName: customer.name, type: "customer", col: -1, value: customer.effective_avg ?? customer.avg_amount ?? null });
            customer.amounts.forEach((value, col) => targets.push({ row: customer.row_key, entityName: customer.name, type: "customer", col, value }));
        });
        for (let col = 0; col < weekCount; col += 1) {
            targets.push({ row: "__other_in", entityName: "__other_in", type: "customer", col, value: data.other_cash_in[col] ?? null });
        }
        data.vendors.forEach((vendor) => {
            targets.push({ row: vendor.row_key, entityName: vendor.name, type: "vendor", col: -2, value: vendor.effective_day ?? vendor.day_of_month ?? null });
            targets.push({ row: vendor.row_key, entityName: vendor.name, type: "vendor", col: -1, value: vendor.effective_avg ?? vendor.avg_amount ?? null });
            vendor.amounts.forEach((value, col) => targets.push({ row: vendor.row_key, entityName: vendor.name, type: "vendor", col, value }));
        });
        for (let col = 0; col < weekCount; col += 1) {
            targets.push({ row: "__other_out", entityName: "__other_out", type: "vendor", col, value: data.other_cash_out[col] ?? null });
        }
        return targets;
    }, [data]);

    const sameCell = (
        a: Pick<EditingCell, "row" | "type" | "col">,
        b: Pick<EditingCell, "row" | "type" | "col">,
    ) => (
        a.row === b.row && a.type === b.type && a.col === b.col
    );

    const findMoveTarget = (current: EditingCell, direction: "left" | "right" | "up" | "down"): EditTarget | null => {
        const targets = buildEditTargets();
        if (!targets.length) return null;
        if (direction === "left" || direction === "right") {
            const idx = targets.findIndex((target) => sameCell(target, current));
            if (idx < 0) return null;
            const nextIdx = direction === "right" ? idx + 1 : idx - 1;
            return targets[nextIdx] ?? null;
        }
        const columnTargets = targets.filter((target) => target.col === current.col);
        const idx = columnTargets.findIndex((target) => sameCell(target, current));
        if (idx < 0) return null;
        const nextIdx = direction === "down" ? idx + 1 : idx - 1;
        return columnTargets[nextIdx] ?? null;
    };

    const recomputeOptimisticTotals = (forecast: ForecastData) => {
        const weekCount = forecast.week_headers.length;
        const sumMoney = (values: Array<number | null | undefined>) => {
            if (values.some((value) => value == null)) return null;
            return roundCurrency(values.reduce<number>((sum, value) => sum + (value ?? 0), 0));
        };
        forecast.total_cash_in = Array.from({ length: weekCount }, (_, idx) => sumMoney([
            ...forecast.customers.map((row) => row.amounts[idx]),
            forecast.other_cash_in[idx],
        ]));
        forecast.total_cash_out = Array.from({ length: weekCount }, (_, idx) => sumMoney([
            ...forecast.vendors.map((row) => row.amounts[idx]),
            forecast.other_cash_out[idx],
        ]));
        const beginning = [...forecast.beginning_balances];
        const ending = [...forecast.ending_balances];
        const reanchorIndex = forecast.forecast_reanchored_to_control === true &&
            forecast.lookback >= 0 && forecast.lookback < weekCount
            ? forecast.lookback
            : -1;
        const authoritativeReanchor = reanchorIndex >= 0
            ? beginning[reanchorIndex]
            : null;
        for (let idx = 0; idx < weekCount; idx += 1) {
            if (idx === reanchorIndex) beginning[idx] = authoritativeReanchor;
            const beginningValue = beginning[idx];
            const cashIn = forecast.total_cash_in[idx];
            const cashOut = forecast.total_cash_out[idx];
            beginning[idx] = beginningValue == null ? null : roundCurrency(beginningValue);
            ending[idx] = beginningValue == null || cashIn == null || cashOut == null
                ? null
                : roundCurrency(beginningValue + cashIn - cashOut);
            if (idx + 1 < weekCount && idx + 1 !== reanchorIndex) {
                beginning[idx + 1] = ending[idx];
            }
        }
        forecast.beginning_balances = beginning;
        forecast.ending_balances = ending;
    };

    const applyOptimisticEdit = (token: OwnerToken, cell: EditingCell, amount: number) => {
        const current = ownedForecastStateRef.current;
        if (
            !current ||
            current.companyId !== token.companyId ||
            current.requestGeneration !== token.requestGeneration
        ) return;
        const updated = JSON.parse(JSON.stringify(current.forecast)) as ForecastData;
        if (cell.row === "__beginning_balance") {
            updated.beginning_balances[cell.col] = amount;
            recomputeOptimisticTotals(updated);
        } else if (cell.row === "__other_in") {
            updated.other_cash_in[cell.col] = amount;
            recomputeOptimisticTotals(updated);
        } else if (cell.row === "__other_out") {
            updated.other_cash_out[cell.col] = amount;
            recomputeOptimisticTotals(updated);
        } else {
            const rows = cell.type === "customer" ? updated.customers : updated.vendors;
            const target = rows.find((row) => row.row_key === cell.row);
            if (target) {
                if (cell.col === -2) {
                    target.day_of_month = amount;
                    target.effective_day = amount;
                } else if (cell.col === -1) {
                    target.avg_amount = amount;
                    target.effective_avg = amount;
                } else {
                    target.amounts[cell.col] = amount;
                    recomputeOptimisticTotals(updated);
                }
            }
        }
        storeVisibleState(token, { ...current, forecast: updated, exportId: null });
    };

    const setSavingDelta = (token: OwnerToken, delta: number) => {
        if (!isCurrentOwner(token)) return;
        const wasSaving = savingCountRef.current > 0;
        savingCountRef.current = Math.max(0, savingCountRef.current + delta);
        const nowSaving = savingCountRef.current > 0;
        setSaving(nowSaving);
        // On the active→idle transition, briefly flash "All changes saved".
        if (wasSaving && !nowSaving) {
            setSavedFlash(true);
            if (savedFlashTimerRef.current) clearTimeout(savedFlashTimerRef.current);
            savedFlashTimerRef.current = setTimeout(() => {
                if (!isCurrentOwner(token)) return;
                setSavedFlash(false);
                savedFlashTimerRef.current = null;
            }, 2000);
        }
    };

    const commitEdit = async (
        nextTarget?: EditTarget | null,
        options: { token?: OwnerToken; allowDuringExport?: boolean; skipAdoption?: boolean } = {},
    ) => {
        const token = options.token ?? ownerRef.current;
        if (!sameOwnerToken(uiOwner, token)) return;
        const currentEditing = editingRef.current;
        const stateAtAdmission = ownedForecastStateRef.current;
        if (
            !currentEditing ||
            !token.companyId ||
            !stateAtAdmission ||
            stateAtAdmission.companyId !== token.companyId ||
            stateAtAdmission.requestGeneration !== token.requestGeneration
        ) {
            if (nextTarget && isCurrentOwner(token)) openEditTarget(nextTarget);
            return;
        }

        const rawValue = editValueRef.current.trim();
        if (rawValue === "" && currentEditing.originalValue == null) {
            if (nextTarget) {
                skipNextBlurRef.current = true;
                if (skipBlurTimerRef.current) clearTimeout(skipBlurTimerRef.current);
                skipBlurTimerRef.current = setTimeout(() => {
                    if (!isCurrentOwner(token)) return;
                    skipNextBlurRef.current = false;
                    skipBlurTimerRef.current = null;
                }, 50);
                openEditTarget(nextTarget);
            } else {
                cancelEdit();
            }
            return;
        }
        const amount = rawValue === "" ? 0 : Number.parseFloat(rawValue);
        const safeAmount = Number.isFinite(amount) ? amount : 0;
        const commitKey = `${currentEditing.type}|${currentEditing.row}|${currentEditing.col}|${rawValue}`;
        const existing = commitPromisesRef.current.get(commitKey);
        if (existing) return existing;

        const snapshotBeforeEdit = stateAtAdmission.forecast;
        const entityName =
            currentEditing.row === "__other_in" ? "Other Cash In" :
                currentEditing.row === "__other_out" ? "Non Routine Expenses" :
                    currentEditing.entityName;
        const weekEnding =
            currentEditing.row === "__beginning_balance" ? "9995-12-31"
                : currentEditing.col === -2 ? "9998-12-31"
                    : currentEditing.col === -1 ? "9999-12-31"
                        : stateAtAdmission.forecast.week_headers[currentEditing.col];
        const mutationKey = `override:${currentEditing.type}:${currentEditing.row}:${currentEditing.col}`;

        const operation = registerMutation(token, async (mutationEpoch) => {
            applyOptimisticEdit(token, currentEditing, safeAmount);
            setSavingDelta(token, 1);
            try {
                await apiClient.post("/cashflow-forecast/override", {
                    company_id: token.companyId,
                    entity_name: entityName,
                    entity_type: currentEditing.type,
                    week_ending: weekEnding,
                    override_amount: safeAmount,
                });
                if (!isCurrentOwner(token)) return;
                if (!editingRef.current && !options.skipAdoption) {
                    await fetchAndAdoptForecast(token, {
                        expectedMutationEpoch: mutationEpoch,
                        clearMutationState: true,
                        adoptingMutationEpoch: mutationEpoch,
                        adoptingMutationKey: mutationKey,
                    });
                }
            } catch (caught: unknown) {
                const coordinator = coordinatorFor(token);
                const current = ownedForecastStateRef.current;
                if (
                    coordinator?.mutationEpoch === mutationEpoch &&
                    current?.companyId === token.companyId &&
                    current.requestGeneration === token.requestGeneration
                ) {
                    storeVisibleState(token, {
                        ...current,
                        forecast: snapshotBeforeEdit,
                        exportId: null,
                    });
                }
                if (isCurrentOwner(token)) setError(getErrorMessage(caught, "Save failed"));
                throw caught;
            } finally {
                committingKeysRef.current.delete(commitKey);
                setSavingDelta(token, -1);
            }
        }, { operationKey: mutationKey, allowDuringExport: options.allowDuringExport });

        if (!operation) return;
        committingKeysRef.current.add(commitKey);

        if (nextTarget) {
            skipNextBlurRef.current = true;
            if (skipBlurTimerRef.current) clearTimeout(skipBlurTimerRef.current);
            skipBlurTimerRef.current = setTimeout(() => {
                if (!isCurrentOwner(token)) return;
                skipNextBlurRef.current = false;
                skipBlurTimerRef.current = null;
            }, 50);
            openEditTarget(nextTarget);
        } else {
            cancelEdit();
        }

        const completion = operation
            .catch(() => undefined)
            .finally(() => {
                if (commitPromisesRef.current.get(commitKey) === completion) {
                    commitPromisesRef.current.delete(commitKey);
                }
            });
        commitPromisesRef.current.set(commitKey, completion);
        return completion;
    };

    const saveEdit = () => {
        void commitEdit();
    };

    const handleEditBlur = () => {
        if (skipNextBlurRef.current) {
            skipNextBlurRef.current = false;
            return;
        }
        saveEdit();
    };

    const handleKeyDown = (e: React.KeyboardEvent) => {
        const currentEditing = editingRef.current;
        if (e.key === "Enter") {
            e.preventDefault();
            void commitEdit(currentEditing ? findMoveTarget(currentEditing, e.shiftKey ? "up" : "down") : null);
        }
        if (e.key === "Escape") cancelEdit();
        if (e.key === "Tab") {
            e.preventDefault();
            void commitEdit(currentEditing ? findMoveTarget(currentEditing, e.shiftKey ? "left" : "right") : null);
        }
    };

    // ── Add new entity ──────────────────────────────────────────────────
    useEffect(() => {
        if (addingEntity && addEntityRef.current) {
            addEntityRef.current.focus();
        }
    }, [addingEntity]);

    const addEntity = async (type: "customer" | "vendor") => {
        const token = ownerRef.current;
        const stateAtAdmission = ownedForecastStateRef.current;
        if (
            !sameOwnerToken(uiOwner, token) ||
            !newEntityName.trim() ||
            !token.companyId ||
            !stateAtAdmission ||
            stateAtAdmission.companyId !== token.companyId ||
            stateAtAdmission.requestGeneration !== token.requestGeneration
        ) return;
        const name = newEntityName.trim();
        const existingRows = type === "customer"
            ? stateAtAdmission.forecast.customers
            : stateAtAdmission.forecast.vendors;
        const occurrence = existingRows.filter((row) => row.name === name).length;
        const mutationKey = `add-entity:${type}:${name}`;
        const operation = registerMutation(token, async () => {
            await apiClient.post("/cashflow-forecast/override", {
                company_id: token.companyId,
                entity_name: name,
                entity_type: type,
                week_ending: "9998-12-31",
                override_amount: 1,
            });
            if (!isCurrentOwner(token)) return;
            const current = ownedForecastStateRef.current;
            if (!current) return;
            const updated = JSON.parse(JSON.stringify(current.forecast)) as ForecastData;
            const resolvedEntityKey = `name:${encodeURIComponent(name)}:occurrence:${occurrence}`;
            const newRow: EntityRow = {
                row_key: `${type}:${resolvedEntityKey}`,
                name,
                day_of_month: 1,
                effective_day: 1,
                avg_amount: 0,
                effective_avg: 0,
                amounts: new Array(updated.week_headers.length).fill(0),
                sort_order: 999,
            };
            if (type === "customer") updated.customers.push(newRow);
            else updated.vendors.push(newRow);
            storeVisibleState(token, { ...current, forecast: updated, exportId: null });
            setAddingEntity(null);
            setNewEntityName("");
        }, { operationKey: mutationKey });
        if (!operation) return;
        try {
            await operation;
        } catch (caught: unknown) {
            if (isCurrentOwner(token)) setError(getErrorMessage(caught, "Failed to add"));
        }
    };

    const handleAddKeyDown = (e: React.KeyboardEvent, type: "customer" | "vendor") => {
        if (e.key === "Enter") { e.preventDefault(); void addEntity(type); }
        if (e.key === "Escape") { setAddingEntity(null); setNewEntityName(""); }
    };

    // ── Drag & Drop reorder ─────────────────────────────────────────────
    const handleDragStart = (type: "customer" | "vendor", idx: number) => {
        setDragType(type);
        setDragIdx(idx);
    };

    const handleDragOver = (e: React.DragEvent, idx: number) => {
        e.preventDefault();
        setDragOverIdx(idx);
    };

    const handleDrop = async (type: "customer" | "vendor", idx: number) => {
        const token = ownerRef.current;
        const stateAtAdmission = ownedForecastStateRef.current;
        if (
            !sameOwnerToken(uiOwner, token) ||
            dragIdx === null ||
            dragType !== type ||
            !token.companyId ||
            !stateAtAdmission ||
            stateAtAdmission.companyId !== token.companyId ||
            stateAtAdmission.requestGeneration !== token.requestGeneration
        ) return;

        // Snapshot last-known-good for rollback, then optimistically reorder.
        const snapshot = stateAtAdmission.forecast;
        const updated = JSON.parse(JSON.stringify(snapshot)) as ForecastData;
        const list = updated[type === "customer" ? "customers" : "vendors"];
        const [moved] = list.splice(dragIdx, 1);
        list.splice(idx, 0, moved);
        const mutationKey = `reorder:${type}`;
        const operation = registerMutation(token, async (mutationEpoch) => {
            const current = ownedForecastStateRef.current;
            if (current && isCurrentOwner(token)) {
                storeVisibleState(token, { ...current, forecast: updated, exportId: null });
            }
            setSavingDelta(token, 1);
            try {
                const results = await Promise.allSettled(
                    list.map((entity, order) =>
                        apiClient.post("/cashflow-forecast/override", {
                            company_id: token.companyId,
                            entity_name: entity.name,
                            entity_type: type,
                            week_ending: "9996-12-31",
                            override_amount: order,
                        }),
                    ),
                );
                const firstRejection = results.find(
                    (result): result is PromiseRejectedResult => result.status === "rejected",
                );
                if (firstRejection) throw firstRejection.reason;
            } catch (caught: unknown) {
                const coordinator = coordinatorFor(token);
                const currentState = ownedForecastStateRef.current;
                if (
                    coordinator?.mutationEpoch === mutationEpoch &&
                    currentState &&
                    isCurrentOwner(token)
                ) {
                    storeVisibleState(token, {
                        ...currentState,
                        forecast: snapshot,
                        exportId: null,
                    });
                }
                throw caught;
            } finally {
                setSavingDelta(token, -1);
            }
        }, { operationKey: mutationKey });
        if (!operation) return;
        setDragIdx(null);
        setDragType(null);
        setDragOverIdx(null);
        try {
            await operation;
        } catch (caught: unknown) {
            if (isCurrentOwner(token)) {
                setError(getErrorMessage(caught, "Could not save the new order — reverted."));
            }
        }
    };

    const handleDragEnd = () => {
        setDragIdx(null);
        setDragType(null);
        setDragOverIdx(null);
    };

    const resetSpreadForm = useCallback(() => {
        setSpreadError(null);
        setSpreadForm({
            entity_name: "",
            entity_type: "vendor",
            total_amount: "",
            periods: "6",
            cadence: "weekly",
            start_week_ending: data?.week_headers?.[0] || "",
            day_of_month: "15",
            note: "",
        });
    }, [data?.week_headers]);

    const submitSpread = async () => {
        const token = ownerRef.current;
        const stateAtAdmission = ownedForecastStateRef.current;
        if (
            !sameOwnerToken(uiOwner, token) ||
            !token.companyId ||
            !data ||
            spreadSaving ||
            stateAtAdmission?.companyId !== token.companyId ||
            stateAtAdmission.requestGeneration !== token.requestGeneration
        ) return;
        const form = { ...spreadForm };
        const mutationKey = `spread:${form.entity_type}:${form.entity_name.trim()}`;
        const operation = registerMutation(token, async (mutationEpoch) => {
            if (isCurrentOwner(token)) {
                setSpreadSaving(true);
                setSpreadError(null);
            }
            await apiClient.post("/cashflow-forecast/spread-balance", {
                company_id: token.companyId,
                entity_name: form.entity_name.trim(),
                entity_type: form.entity_type,
                total_amount: parseFloat(form.total_amount) || 0,
                periods: parseInt(form.periods, 10) || 0,
                cadence: form.cadence,
                start_week_ending: form.start_week_ending,
                day_of_month: parseInt(form.day_of_month, 10) || 15,
                note: form.note.trim() || null,
            });
            if (!isCurrentOwner(token)) return;
            await fetchAndAdoptForecast(token, {
                expectedMutationEpoch: mutationEpoch,
                clearMutationState: true,
                adoptingMutationEpoch: mutationEpoch,
                adoptingMutationKey: mutationKey,
            });
            if (isCurrentOwner(token)) {
                setShowSpreadModal(false);
                resetSpreadForm();
            }
        }, { operationKey: mutationKey });
        if (!operation) return;
        try {
            await operation;
        } catch (caught: unknown) {
            if (isCurrentOwner(token)) setSpreadError(getErrorMessage(caught, "Failed to create spread"));
        } finally {
            if (isCurrentOwner(token)) setSpreadSaving(false);
        }
    };

    // ── Remove entity ───────────────────────────────────────────────────
    const removeEntity = async (rowKey: string, name: string, type: "customer" | "vendor") => {
        const token = ownerRef.current;
        const stateAtAdmission = ownedForecastStateRef.current;
        if (
            !sameOwnerToken(uiOwner, token) ||
            !token.companyId ||
            !stateAtAdmission ||
            stateAtAdmission.companyId !== token.companyId ||
            stateAtAdmission.requestGeneration !== token.requestGeneration
        ) return;
        const snapshot = stateAtAdmission.forecast;
        const mutationKey = `remove:${type}:${rowKey}`;
        const operation = registerMutation(token, async (mutationEpoch) => {
            const current = ownedForecastStateRef.current;
            if (current && isCurrentOwner(token)) {
                const updated = JSON.parse(JSON.stringify(current.forecast)) as ForecastData;
                if (type === "customer") {
                    updated.customers = updated.customers.filter((customer) => customer.row_key !== rowKey);
                } else {
                    updated.vendors = updated.vendors.filter((vendor) => vendor.row_key !== rowKey);
                }
                storeVisibleState(token, { ...current, forecast: updated, exportId: null });
            }
            setSavingDelta(token, 1);
            try {
                await apiClient.post("/cashflow-forecast/override", {
                    company_id: token.companyId,
                    entity_name: name,
                    entity_type: type,
                    week_ending: "9997-12-31",
                    override_amount: 1,
                });
            } catch (caught: unknown) {
                const coordinator = coordinatorFor(token);
                const currentState = ownedForecastStateRef.current;
                if (
                    coordinator?.mutationEpoch === mutationEpoch &&
                    currentState &&
                    isCurrentOwner(token)
                ) {
                    storeVisibleState(token, {
                        ...currentState,
                        forecast: snapshot,
                        exportId: null,
                    });
                }
                throw caught;
            } finally {
                setSavingDelta(token, -1);
            }
        }, { operationKey: mutationKey });
        if (!operation) return;
        try {
            await operation;
        } catch (caught: unknown) {
            if (isCurrentOwner(token)) {
                setError(getErrorMessage(caught, "Could not remove entity — restored."));
            }
        }
    };

    const handleBannerAddRow = useCallback((entity: UnmappedEntity) => {
        const token = ownerRef.current;
        if (!sameOwnerToken(uiOwner, token)) {
            return Promise.reject(new Error("The selected company changed before this row could be added."));
        }
        const mutationKey = `add-row:${entity.entity_type}:${entity.entity_name}`;
        const operation = registerMutation(token, async (mutationEpoch) => {
            if (!token.companyId) return;
            await addForecastRow(token.companyId, entity.entity_name, entity.entity_type);
            if (!isCurrentOwner(token)) return;
            await fetchAndAdoptForecast(token, {
                expectedMutationEpoch: mutationEpoch,
                clearMutationState: true,
                adoptingMutationEpoch: mutationEpoch,
                adoptingMutationKey: mutationKey,
            });
            if (isCurrentOwner(token)) setIntegrityRefreshKey((key) => key + 1);
        }, { operationKey: mutationKey });
        if (!operation) {
            return Promise.reject(new Error("The forecast is busy finalizing a download."));
        }
        return operation.catch((caught: unknown) => {
            if (isCurrentOwner(token)) {
                setError(getErrorMessage(caught, "Couldn't add the forecast row. Please try again."));
            }
            throw caught;
        });
    }, [fetchAndAdoptForecast, isCurrentOwner, registerMutation, uiOwner]);

    const resetActualsReview = async () => {
        const token = ownerRef.current;
        if (!sameOwnerToken(uiOwner, token)) return;
        const weekEnding = actualsWeek;
        const mutationKey = `actuals-reset:${weekEnding || "latest"}`;
        const operation = registerMutation(token, async () => {
            if (!token.companyId || !weekEnding) return;
            if (isCurrentOwner(token)) setActualsLoading(true);
            try {
                await apiClient.delete("/cashflow-forecast/actuals/reset", {
                    params: { company_id: token.companyId, week_ending: weekEnding },
                });
                if (!isCurrentOwner(token)) return;
                await pullActualsData(token, weekEnding);
            } finally {
                if (isCurrentOwner(token)) setActualsLoading(false);
            }
        }, { operationKey: mutationKey });
        if (!operation) return;
        try {
            await operation;
        } catch (caught: unknown) {
            if (isCurrentOwner(token)) {
                setError(getErrorMessage(caught, "Couldn't re-pull actuals from QuickBooks. Please try again."));
            }
        }
    };

    const categorizeActual = async (
        transaction: ActualTransaction,
        entityName: string,
        entityType: string,
    ) => {
        const token = ownerRef.current;
        if (!sameOwnerToken(uiOwner, token)) return;
        const priorTransaction = actuals.find((item) => item.id === transaction.id);
        const mutationKey = `actuals:${transaction.id}`;
        let admittedEpoch = -1;
        const operation = registerMutation(token, async (mutationEpoch) => {
            admittedEpoch = mutationEpoch;
            if (!isCurrentOwner(token)) return;
            setActuals((previous) => previous.map((item) =>
                item.id === transaction.id
                    ? { ...item, entity_name: entityName, entity_type: entityType, category: "categorized" }
                    : item,
            ));
            setActiveDropdown(null);
            setDropdownSearch("");
            dropdownSearchRef.current = "";
            try {
                const result = await apiClient.post("/cashflow-forecast/actuals/categorize", {
                    id: transaction.id,
                    entity_name: entityName,
                    entity_type: entityType,
                    category: "categorized",
                });
                if (!isCurrentOwner(token)) return;
                const coordinator = coordinatorFor(token);
                if (!isLatestMutationForKey(coordinator, mutationKey, mutationEpoch)) return;
                setError(null);
                if (
                    result.data?.cff_unmapped &&
                    (entityType === "customer" || entityType === "vendor")
                ) {
                    setUnmappedWarning({ name: entityName, type: entityType });
                }
            } catch (caught: unknown) {
                const coordinator = coordinatorFor(token);
                if (
                    priorTransaction &&
                    isCurrentOwner(token) &&
                    isLatestMutationForKey(coordinator, mutationKey, mutationEpoch)
                ) {
                    setActuals((previous) => previous.map((item) =>
                        item.id === transaction.id ? priorTransaction : item,
                    ));
                }
                throw caught;
            }
        }, { operationKey: mutationKey });
        if (!operation) return;
        try {
            await operation;
        } catch (caught: unknown) {
            if (
                isCurrentOwner(token) &&
                isLatestMutationForKey(coordinatorFor(token), mutationKey, admittedEpoch)
            ) {
                setError(getApiErrorMessage(caught, "Couldn't categorize this transaction. Please try again."));
            }
        }
    };

    const toggleActualExclusion = async (transaction: ActualTransaction) => {
        const token = ownerRef.current;
        if (!sameOwnerToken(uiOwner, token)) return;
        const nextCategory = transaction.category === "excluded" ? "uncategorized" : "excluded";
        const priorTransaction = actuals.find((item) => item.id === transaction.id);
        const mutationKey = `actuals:${transaction.id}`;
        let admittedEpoch = -1;
        const operation = registerMutation(token, async (mutationEpoch) => {
            admittedEpoch = mutationEpoch;
            if (!isCurrentOwner(token)) return;
            setActuals((previous) => previous.map((item) =>
                item.id === transaction.id ? { ...item, category: nextCategory } : item,
            ));
            try {
                await apiClient.post("/cashflow-forecast/actuals/categorize", {
                    id: transaction.id,
                    entity_name: transaction.entity_name || "__excluded",
                    entity_type: transaction.direction === "in" ? "customer" : "vendor",
                    category: nextCategory,
                });
            } catch (caught: unknown) {
                const coordinator = coordinatorFor(token);
                if (
                    priorTransaction &&
                    isCurrentOwner(token) &&
                    isLatestMutationForKey(coordinator, mutationKey, mutationEpoch)
                ) {
                    setActuals((previous) => previous.map((item) =>
                        item.id === transaction.id ? priorTransaction : item,
                    ));
                }
                throw caught;
            }
        }, { operationKey: mutationKey });
        if (!operation) return;
        try {
            await operation;
        } catch (caught: unknown) {
            if (
                isCurrentOwner(token) &&
                isLatestMutationForKey(coordinatorFor(token), mutationKey, admittedEpoch)
            ) {
                setError(getErrorMessage(caught, "Couldn't update this transaction. Please try again."));
            }
        }
    };

    const handleForecastDownload = (format: CashflowForecastExportFormat) => {
        const token = ownerRef.current;
        if (!sameOwnerToken(uiOwner, token)) return;
        const coordinator = coordinatorFor(token);
        if (!coordinator || !token.companyId || coordinator.exportLocked) return;

        // This ref mutation is the shared cross-format linearization point. It
        // happens before React state, promises, or network work can yield.
        coordinator.exportLocked = true;
        coordinator.mutationAdmissionOpen = false;
        setExportBusyFormat(format);
        setExportError(null);

        const exportTask = (async () => {
            let current = ownedForecastStateRef.current;
            const currentOwnsState =
                current?.companyId === token.companyId &&
                current.requestGeneration === token.requestGeneration;
            const cleanFastPath = Boolean(
                currentOwnsState &&
                current?.exportId &&
                !editingRef.current &&
                coordinator.pendingMutations.size === 0 &&
                !coordinator.needsAdoption &&
                !hasUncorrectedMutationFailure(coordinator)
            );

            if (!cleanFastPath) {
                if (editingRef.current) {
                    await commitEdit(undefined, {
                        token,
                        allowDuringExport: true,
                        skipAdoption: true,
                    });
                }
                while (coordinator.pendingMutations.size > 0) {
                    await Promise.allSettled([...coordinator.pendingMutations]);
                }
                if (coordinator.refreshPromise) await coordinator.refreshPromise;
                if (!isCurrentOwner(token)) return;
                if (hasUncorrectedMutationFailure(coordinator)) {
                    throw new Error(
                        "A forecast change failed. Correct or retry it before downloading.",
                    );
                }

                const finalEpoch = coordinator.mutationEpoch;
                const adopted = await fetchAndAdoptForecast(token, {
                    expectedMutationEpoch: finalEpoch,
                    clearMutationState: true,
                });
                if (!adopted || !isCurrentOwner(token)) return;
                current = ownedForecastStateRef.current;
            }

            if (
                !current ||
                current.companyId !== token.companyId ||
                current.requestGeneration !== token.requestGeneration
            ) return;
            if (!current.exportId || current.exportCaptureState !== "ready") {
                throw new Error(exportErrorMessage(current.exportError));
            }

            await downloadCashflowForecastExport({
                exportId: current.exportId,
                expectedCompanyId: token.companyId,
                format,
                isCurrentOwner: () =>
                    isCurrentOwner(token) &&
                    coordinatorRef.current === coordinator &&
                    coordinator.exportLocked,
            });
        })();

        const trackedExport = exportTask
            .catch((caught: unknown) => {
                if (isCurrentOwner(token)) {
                    setExportError(getErrorMessage(caught, "Forecast download failed. Please retry."));
                }
            })
            .finally(() => {
                coordinator.exportLocked = false;
                coordinator.mutationAdmissionOpen = true;
                if (isCurrentOwner(token) && coordinatorRef.current === coordinator) {
                    setExportBusyFormat(null);
                }
                if (coordinator.exportPromise === trackedExport) coordinator.exportPromise = null;
            });
        coordinator.exportPromise = trackedExport;
    };

    /* ── render helpers ────────────────────────────────────────────────── */

    const renderCell = (
        row: string,
        type: "customer" | "vendor",
        col: number,
        value: number | null,
        editable: boolean = true,
        entityName: string = row,
    ) => {
        const isEditing = editing?.row === row && editing?.type === type && editing?.col === col;

        if (isEditing) {
            return (
                <td key={col} className="px-1 py-1">
                    <input
                        ref={inputRef}
                        type="number"
                        step="0.01"
                        value={editValue}
                        onChange={(e) => updateEditValue(e.target.value)}
                        onBlur={handleEditBlur}
                        onKeyDown={handleKeyDown}
                        className="w-full px-2 py-1 text-right text-xs bg-primary/10 border border-primary rounded font-mono text-text focus:outline-none"
                    />
                </td>
            );
        }

        return (
            <td
                key={col}
                onMouseDown={editable ? (e) => {
                    e.preventDefault();
                    const target = { row, entityName, type, col, value };
                    if (editingRef.current && !sameCell(editingRef.current, target)) {
                        void commitEdit(target);
                    } else {
                        startEdit(row, type, col, value, entityName);
                    }
                } : undefined}
                className={`px-2 py-1.5 text-right text-xs whitespace-nowrap ${editable
                    ? "cursor-pointer hover:bg-primary/5 transition-colors"
                    : ""
                    } ${value == null || value === 0 ? "text-textMuted/30" : "text-text"}`}
            >
                {col === -2
                    ? (value == null || value === 0 ? "—" : value.toString())
                    : fmt(value)
                }
            </td>
        );
    };

    const weeks = data?.week_headers.length || 13;

    // Effectively empty: a successful load with no entities on either side and no
    // manual cash activity — render guidance instead of a near-blank grid.
    const isForecastEmpty = !!data
        && data.customers.length === 0
        && data.vendors.length === 0
        && (data.other_cash_in ?? []).every((v) => v === 0)
        && (data.other_cash_out ?? []).every((v) => v === 0);

    // 2026-07-09 honest-failure batch (AC-C5): the backend's honest-gap ledger, narrowed
    // once here so the banners below read cleanly. (no_forecast_row entities get their own
    // actionable UnmappedEntitiesBanner.)
    const unknownTypeGap = data?.unmatched?.reasons?.unknown_entity_type;
    const beyondHorizon =
        data?.beyond_horizon && data.beyond_horizon.item_count > 0 ? data.beyond_horizon : null;
    const nonRecurring = data?.non_recurring_history ?? [];

    if (!companyId) {
        return (
            <div className="text-center py-12 text-textMuted border border-dashed border-border rounded-xl">
                Select a company from the company menu in the top bar to view the cash flow forecast.
            </div>
        );
    }

    return (
        <div className="flex h-full -mx-6 -my-6" style={{ width: 'calc(100% + 3rem)', height: 'calc(100% + 3rem)' }}>
            {/* ── Sidebar: Entity Reference Panel ────────────────────────── */}
            {sidebarOpen && data && (
                <div className="hidden md:block w-56 flex-shrink-0 border-r border-border bg-surface overflow-y-auto">
                    <div className="px-4 py-3 border-b border-border flex items-center justify-between">
                        <h2 className="text-sm font-bold text-text">Entity Reference</h2>
                        <button
                            onClick={() => setSidebarOpen(false)}
                            className="text-textMuted hover:text-text text-xs"
                        >✕</button>
                    </div>

                    {/* Customers */}
                    <div className="p-3">
                        <h3 className="text-xs font-bold text-accentIndigo uppercase tracking-wider mb-2">
                            Customers
                        </h3>
                        <table className="w-full text-xs">
                            <thead>
                                <tr className="text-textMuted">
                                    <th className="text-left py-1">Name</th>
                                    <th className="text-right py-1 w-10" title="Calendar day-of-month this cash activity typically lands within the Friday-ending week">Day</th>
                                    <th className="text-right py-1 w-20" title="Average monthly cash represented by this customer">Avg/Mo</th>
                                </tr>
                            </thead>
                            <tbody>
                                {data.customers.map((c) => (
                                    <tr key={c.row_key} className="border-t border-border/50">
                                        <td className="py-1 text-text truncate max-w-[120px]" title={c.name}>
                                            <div className="flex items-center gap-1">
                                                {c.source === "invoice" && <span className="text-[9px] px-1 py-0.5 rounded bg-primary/20 text-accentIndigo font-bold" title={`${c.open_invoices} open invoice(s)`}>INV</span>}
                                                {c.source === "pattern" && <span className="text-[9px] px-1 py-0.5 rounded bg-surfaceAlt text-textMuted font-bold">PAT</span>}
                                                {c.source === "manual" && <span className="text-[9px] px-1 py-0.5 rounded bg-accentPurple/20 text-accentPurple font-bold">MAN</span>}
                                                <span className="truncate">{c.name}</span>
                                            </div>
                                        </td>
                                        <td className="py-1 text-right text-textMuted" title="Typical calendar day-of-month this cash hits inside the Friday-ending week">
                                            {c.effective_day ?? c.day_of_month ?? "—"}
                                        </td>
                                        <td className="py-1 text-right text-text font-medium" title={c.source === "invoice" ? `Open balance: ${fmt(c.total_open_balance)}` : "Average monthly cash amount"}>
                                            {fmt(c.effective_avg ?? c.avg_amount)}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>

                    {/* Vendors */}
                    <div className="p-3 border-t border-border">
                        <h3 className="text-xs font-bold text-accentAmber uppercase tracking-wider mb-2">
                            Vendors
                        </h3>
                        <table className="w-full text-xs">
                            <thead>
                                <tr className="text-textMuted">
                                    <th className="text-left py-1">Name</th>
                                    <th className="text-right py-1 w-10" title="Calendar day-of-month this cash activity typically lands within the Friday-ending week">Day</th>
                                    <th className="text-right py-1 w-20" title="Average monthly cash represented by this vendor">Avg/Mo</th>
                                </tr>
                            </thead>
                            <tbody>
                                {data.vendors.map((v) => (
                                    <tr key={v.row_key} className="border-t border-border/50">
                                        <td className="py-1 text-text truncate max-w-[120px]" title={v.name}>
                                            <div className="flex items-center gap-1">
                                                {v.source === "invoice" && <span className="text-[9px] px-1 py-0.5 rounded bg-accentAmber/20 text-accentAmber font-bold" title={`${v.open_invoices} open bill(s)`}>INV</span>}
                                                {v.source === "pattern" && <span className="text-[9px] px-1 py-0.5 rounded bg-surfaceAlt text-textMuted font-bold">PAT</span>}
                                                {v.source === "manual" && <span className="text-[9px] px-1 py-0.5 rounded bg-accentPurple/20 text-accentPurple font-bold">MAN</span>}
                                                <span className="truncate">{v.name}</span>
                                            </div>
                                        </td>
                                        <td className="py-1 text-right text-textMuted" title="Typical calendar day-of-month this cash hits inside the Friday-ending week">
                                            {v.effective_day ?? v.day_of_month ?? "—"}
                                        </td>
                                        <td className="py-1 text-right text-text font-medium" title={v.source === "invoice" ? `Open balance: ${fmt(v.total_open_balance)}` : "Average monthly cash amount"}>
                                            {fmt(v.effective_avg ?? v.avg_amount)}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}

            {/* ── Main Content ───────────────────────────────────────────── */}
            <div className="flex-1 overflow-hidden flex flex-col p-3">
                {/* Header */}
                <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between flex-shrink-0">
                    <div>
                        <h1 className="text-2xl font-bold text-text">Rolling 13-Week Cash Flow Forecast</h1>
                        <p className="text-textMuted text-sm mt-0.5">
                            Friday-to-Friday cash calendar with entity day-of-month triggers and average cash activity • Click any cell to override
                            {snapshotAge != null && snapshotAge > 0 && (
                                <span className="ml-2 text-xs opacity-70">
                                    • Last refreshed {snapshotAge < 60 ? 'just now' : snapshotAge < 3600 ? `${Math.round(snapshotAge / 60)}m ago` : snapshotAge < 86400 ? `${Math.round(snapshotAge / 3600)}h ago` : `${Math.round(snapshotAge / 86400)}d ago`}
                                </span>
                            )}
                        </p>
                        <div className="mt-2 rounded-lg border border-border bg-surfaceAlt/70 px-3 py-2 text-xs text-textMuted max-w-3xl">
                            Need to spread an accrued or deferred balance without overwriting the base forecast? Ask Vesper Bot something like:
                            {" "}
                            <span className="text-text">
                                &quot;Spread $24,000 of insurance accrual over the next 6 weeks as cash out starting May 1.&quot;
                            </span>
                        </div>
                    </div>
                    <div className="flex gap-2 flex-wrap items-center">
                        {(["excel", "pdf"] as const).map((format) => (
                            <button
                                key={format}
                                onClick={() => handleForecastDownload(format)}
                                disabled={exportBusyFormat !== null}
                                title={
                                    visibleState?.exportCaptureState === "building"
                                        ? "The forecast export is still building — click to check again"
                                        : visibleState?.exportCaptureState === "unavailable"
                                            ? exportErrorMessage(visibleState.exportError)
                                            : undefined
                                }
                                className="inline-flex items-center gap-2 rounded-md border border-border bg-surface px-3 py-2 text-sm font-medium text-text hover:bg-surfaceAlt disabled:opacity-50 transition-colors"
                            >
                                {exportBusyFormat !== null ? (
                                    <Loader2 className="h-4 w-4 animate-spin" />
                                ) : (
                                    <Download className="h-4 w-4" />
                                )}
                                {exportBusyFormat !== null
                                    ? "Downloading…"
                                    : `Download forecast (${format === "excel" ? "Excel" : "PDF"})`}
                            </button>
                        ))}
                        <SaveStatus
                            state={saving ? "saving" : savedFlash ? "saved" : "idle"}
                            className="mr-1"
                        />
                        <button
                            onClick={() => setShowSpreadModal(true)}
                            className="px-3 py-2 border border-border rounded-lg text-sm text-text hover:border-primary/30 hover:text-primaryText transition"
                        >
                            + Add Spread
                        </button>
                        {data && (data.uncategorized_actuals ?? 0) > 0 && (
                            <button
                                onClick={() => { void openActualsReview(reviewRequiredWeeks[0]); }}
                                className="px-3 py-2 rounded-lg text-sm font-medium bg-warning/20 text-accentAmber hover:bg-warning/30 border border-warning/30 transition flex items-center gap-1.5"
                            >
                                📥 Categorize Last Week ({data.uncategorized_actuals} pending)
                            </button>
                        )}
                        {data && (data.uncategorized_actuals ?? 0) === 0 && (
                            <button
                                onClick={() => { void openActualsReview(reviewRequiredWeeks[0]); }}
                                className="px-3 py-2 rounded-lg text-sm text-textMuted hover:text-text border border-border hover:border-primary/30 transition flex items-center gap-1.5"
                            >
                                📥 Pull Transactions
                            </button>
                        )}
                        {!sidebarOpen && data && (
                            <button
                                onClick={() => setSidebarOpen(true)}
                                className="px-3 py-2 border border-border rounded-lg text-sm text-textMuted hover:text-text transition"
                            >
                                📊 Reference
                            </button>
                        )}
                        <button
                            onClick={() => { void refreshFromQBO(); }}
                            disabled={refreshing}
                            className="px-3 py-2 border border-border rounded-lg text-sm text-textMuted hover:text-text hover:border-primary/30 transition disabled:opacity-50 flex items-center gap-1.5"
                        >
                            <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
                            {refreshing ? "Refreshing QBO…" : "Refresh from QBO"}
                        </button>
                    </div>
                </div>

                {exportError && (
                    <div
                        role="alert"
                        data-testid="cff-export-error"
                        className="px-4 py-2 mb-4 flex-shrink-0 rounded-lg border border-danger/30 bg-danger/10 text-danger text-sm"
                    >
                        {exportError}
                    </div>
                )}

                {loading && (
                    <div className="flex items-center justify-center flex-1 text-textMuted">
                        Loading forecast…
                    </div>
                )}

                {error && isQboNotConnectedError(loadErrorRaw) && (
                    <QboNotConnected error={loadErrorRaw} />
                )}

                {error && !isQboNotConnectedError(loadErrorRaw) && (
                    <div className="p-4 bg-danger/10 border border-danger/30 rounded-lg text-danger mb-4 flex-shrink-0 flex items-start justify-between gap-3">
                        <span>{error}</span>
                        <button
                            onClick={() => { setError(null); setLoadErrorRaw(null); }}
                            className="text-danger/70 hover:text-danger transition flex-shrink-0"
                            title="Dismiss"
                            aria-label="Dismiss error"
                        >
                            <X className="h-4 w-4" />
                        </button>
                    </div>
                )}

                {data?.data_quality?.is_partial && (
                    <div
                        role="alert"
                        className="px-4 py-2 mb-4 flex-shrink-0 rounded-lg border border-warning/30 bg-warning/10 text-accentAmber text-sm flex items-start gap-2"
                    >
                        <span aria-hidden>⚠️</span>
                        <span>
                            Some QuickBooks data couldn&rsquo;t be loaded, so this forecast may be
                            incomplete — figures shown are partial, not a confirmed zero.
                            {data.data_quality.unavailable_sections.length > 0 && (
                                <> Unavailable: {data.data_quality.unavailable_sections.join(", ")}.</>
                            )}{" "}
                            Try refreshing from QuickBooks.
                        </span>
                    </div>
                )}

                {/* 2026-07-09 honest-failure batch (AC-C5): the backend's honest-gap ledger —
                    the totals below are incomplete by these counted amounts, say so plainly.
                    (no_forecast_row entities get their own actionable banner below.) */}
                {unknownTypeGap && unknownTypeGap.count > 0 && (
                    <div
                        role="alert"
                        className="px-4 py-2 mb-4 flex-shrink-0 rounded-lg border border-warning/30 bg-warning/10 text-accentAmber text-sm flex items-start gap-2"
                    >
                        <span aria-hidden>⚠️</span>
                        <span>
                            {unknownTypeGap.count} categorized item{unknownTypeGap.count === 1 ? "" : "s"} totalling{" "}
                            {fmt(unknownTypeGap.total_amount)} couldn&rsquo;t be placed in the forecast
                            (unrecognized entity type) — the week totals exclude them. Re-categorize to a
                            customer or vendor.
                        </span>
                    </div>
                )}
                {beyondHorizon && (
                    <div
                        className="px-4 py-2 mb-4 flex-shrink-0 rounded-lg border border-border bg-surfaceAlt text-textMuted text-sm flex items-start gap-2"
                    >
                        <span aria-hidden>ℹ️</span>
                        <span>
                            {beyondHorizon.item_count} open item{beyondHorizon.item_count === 1 ? "" : "s"} expected
                            beyond the last forecast week — {fmt(beyondHorizon.cash_in)} in /{" "}
                            {fmt(beyondHorizon.cash_out)} out are not in this grid&rsquo;s totals.
                        </span>
                    </div>
                )}
                {nonRecurring.length > 0 && (
                    <div
                        className="px-4 py-2 mb-4 flex-shrink-0 rounded-lg border border-border bg-surfaceAlt text-textMuted text-sm flex items-start gap-2"
                    >
                        <span aria-hidden>ℹ️</span>
                        <span>
                            {nonRecurring.length} payee{nonRecurring.length === 1 ? "" : "s"} from recent
                            history {nonRecurring.length === 1 ? "is" : "are"} not projected as recurring
                            (one-off, sparse or irregular payments):{" "}
                            {nonRecurring.slice(0, 5).map((payee) => `${payee.name} (${fmt(payee.total_amount)})`).join(", ")}
                            {nonRecurring.length > 5 ? ` and ${nonRecurring.length - 5} more` : ""}. Add a
                            row if any of them will recur.
                        </span>
                    </div>
                )}

                <UnmappedEntitiesBanner
                    // PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-5 AC-2 (F-4 — unmapped callout).
                    companyId={companyId}
                    requestGeneration={renderOwner.requestGeneration}
                    refreshKey={integrityRefreshKey}
                    onAddRow={handleBannerAddRow}
                />

                {data && isForecastEmpty && !loading && !error && (
                    <div className="flex flex-col items-center justify-center flex-1 text-center px-6 py-16 text-textMuted">
                        <div className="flex items-center justify-center w-12 h-12 rounded-full bg-primary/10 text-primaryText mb-4">
                            <Inbox className="w-6 h-6" />
                        </div>
                        <p className="font-medium text-text">No cash activity yet</p>
                        <p className="text-sm text-textMuted mt-1 max-w-md">
                            Refresh from QBO to pull recent transactions, or click a cell to add an override.
                        </p>
                        <Button
                            onClick={() => { void refreshFromQBO(); }}
                            disabled={refreshing}
                            className="mt-4 inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition disabled:opacity-50"
                        >
                            <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
                            {refreshing ? "Refreshing QBO…" : "Refresh from QBO"}
                        </Button>
                    </div>
                )}

                {data && !isForecastEmpty && (
                    <div className="overflow-auto flex-1 min-h-0 rounded-xl border border-border shadow-sm">
                        <div className="px-4 py-2 text-xs text-textMuted border-b border-border bg-surfaceAlt/60">
                            Beginning balance anchor: {fmt(data.bank_balance)} as of{" "}
                            {data.bank_balance_as_of ? fmtDate(data.bank_balance_as_of) : "the prior Friday"}
                            {data.opening_balance_week_ending && (
                                <span>
                                    {" "}for {fmtDate(data.opening_balance_week_ending)} beginning balance
                                </span>
                            )}
                            {(data.lookback ?? 0) > 0 && data.first_forecast_week_ending && data.beginning_balances[data.lookback] != null && (
                                <span className="ml-3 text-textMuted">
                                    First forecast week {fmtDate(data.first_forecast_week_ending)} begins at{" "}
                                    {fmt(data.beginning_balances[data.lookback])} after closed-week activity
                                </span>
                            )}
                            {data.anchor_diagnostics && (
                                <span className={`ml-3 ${anchorSummaryMismatch(data.anchor_diagnostics) ? "text-accentAmber" : "text-textMuted"}`}>
                                    QBO BS requested {fmtDate(data.anchor_diagnostics.requested_end_date)}
                                    {" "} | report end {fmtDate(data.anchor_diagnostics.qbo_header_end_period)}
                                    {" "} | bank rows {data.anchor_diagnostics.bank_accounts_row_count ?? "—"} total{" "}
                                    {fmt(data.anchor_diagnostics.bank_accounts_row_total ?? data.anchor_diagnostics.bank_accounts_summary_amount)}
                                    {data.anchor_diagnostics.cash_account_universe && (
                                        <>
                                            {" "} | cash accounts {data.anchor_diagnostics.cash_account_universe.included_count ?? "—"}
                                            {" "}incl. {data.anchor_diagnostics.cash_account_universe.clearing_count ?? 0} clearing
                                        </>
                                    )}
                                    {(data.anchor_diagnostics.cash_account_universe?.unmatched_balance_sheet_row_count ?? 0) > 0 && (
                                        <span className="ml-2 text-accentAmber">
                                            {data.anchor_diagnostics.cash_account_universe?.unmatched_balance_sheet_row_count} BS cash row(s) unmatched
                                        </span>
                                    )}
                                </span>
                            )}
                            {data.reconciliation_delta && (
                                <span className={`ml-3 ${Math.abs(data.reconciliation_delta.delta) >= 0.01 ? "text-accentAmber" : "text-textMuted"}`}>
                                    Control balance: {fmt(data.reconciliation_delta.anchor_bank_balance)} as of{" "}
                                    {fmtDate(data.reconciliation_delta.anchor_as_of)}
                                    {" "} | Reconciliation delta: {fmt(data.reconciliation_delta.delta)}
                                    {" "}(
                                    BS change {fmt(data.reconciliation_delta.balance_sheet_change)}
                                    {" "}vs actuals {fmt(data.reconciliation_delta.actuals_net_change)}
                                    )
                                </span>
                            )}
                        </div>
                        {reviewRequiredWeeks.length > 0 && (
                            <div className="px-4 py-2 text-xs border-b border-warning/30 bg-warning/10 text-accentAmber flex items-center gap-2 flex-wrap">
                                <span className="font-semibold">Weekly actuals need review:</span>
                                {reviewRequiredWeeks.map((wk) => (
                                    <button
                                        key={wk}
                                        onClick={() => { void openActualsReview(wk); }}
                                        className="rounded border border-warning/30 px-2 py-1 text-xs hover:bg-warning/20 transition"
                                    >
                                        {new Date(wk + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" })}
                                    </button>
                                ))}
                            </div>
                        )}
                        <table className="min-w-full text-xs border-collapse">
                            <thead className="sticky top-0 z-20">
                                {/* Week headers */}
                                <tr className="bg-primary text-white">
                                    <th className="sticky left-0 z-30 bg-primary px-4 py-2.5 text-left font-semibold min-w-[200px] border-r border-white/10">
                                        Entity
                                    </th>
                                    <th className="px-2 py-2.5 text-center font-semibold w-16 border-r border-white/10">
                                        Day
                                    </th>
                                    <th className="px-2 py-2.5 text-right font-semibold w-20 border-r border-white/10">
                                        Avg/Mo
                                    </th>
                                    {data.week_headers.map((wk, i) => (
                                        <th key={i} className="px-2 py-2.5 text-right font-semibold whitespace-nowrap min-w-[90px]">
                                            <div>{new Date(wk + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" })}</div>
                                            {i < (data.lookback ?? 0) && statusByWeek.get(wk) && (
                                                <div className="mt-1 text-[10px] font-medium opacity-80">
                                                    {weekStatusLabel(statusByWeek.get(wk)?.status)}
                                                </div>
                                            )}
                                        </th>
                                    ))}
                                </tr>

                                {/* Beginning balance */}
                                <tr className="bg-surfaceAlt border-b border-border">
                                    <td className="sticky left-0 z-10 bg-surfaceAlt px-4 py-2 font-semibold text-text border-r border-border">
                                        Beginning Bank Balance
                                    </td>
                                    <td className="border-r border-border"></td>
                                    <td className="border-r border-border"></td>
                                    {data.beginning_balances.map((b, i) => {
                                        const isEditing = editing?.row === "__beginning_balance" && editing.col === i;
                                        const isFirst = i === 0;
                                        const isPrior = i < (data.lookback ?? 0);
                                        const canEditBeginning = isFirst && (data.lookback ?? 0) === 0;
                                        return (
                                            <td
                                                key={i}
                                                className={`px-2 py-2 text-right font-medium text-xs ${b != null && b < 0 ? "text-danger" : "text-text"} ${isPrior ? "bg-surfaceAlt/80" : ""} ${canEditBeginning ? "cursor-pointer hover:bg-primary/10" : ""}`}
                                                onMouseDown={canEditBeginning ? (e) => {
                                                    e.preventDefault();
                                                    const target = { row: "__beginning_balance", entityName: "__beginning_balance", type: "balance" as const, col: i, value: b };
                                                    if (editingRef.current && !sameCell(editingRef.current, target)) {
                                                        void commitEdit(target);
                                                    } else {
                                                        startEdit("__beginning_balance", "balance", i, b);
                                                    }
                                                } : undefined}
                                                title={canEditBeginning ? "Click to edit beginning balance" : undefined}
                                            >
                                                {isEditing ? (
                                                    <input
                                                        ref={inputRef}
                                                        type="number"
                                                        value={editValue}
                                                        onChange={(e) => updateEditValue(e.target.value)}
                                                        onBlur={handleEditBlur}
                                                        onKeyDown={handleKeyDown}
                                                        className="w-full px-1 py-0.5 text-right text-xs bg-primary/10 border border-primary rounded text-text focus:outline-none"
                                                    />
                                                ) : (
                                                    fmt(b)
                                                )}
                                            </td>
                                        );
                                    })}
                                </tr>
                            </thead>

                            <tbody>
                                {/* ── CUSTOMERS ── */}
                                <tr className="bg-primary text-white">
                                    <td colSpan={weeks + 3} className="sticky left-0 px-4 py-2 font-bold text-xs tracking-wider uppercase">
                                        Customer Collections (Cash In)
                                    </td>
                                </tr>

                                {data.customers.map((customer, idx) => (
                                    <tr
                                        key={customer.row_key}
                                        data-testid={`forecast-row-${customer.row_key}`}
                                        draggable
                                        onDragStart={() => handleDragStart("customer", idx)}
                                        onDragOver={(e) => handleDragOver(e, idx)}
                                        onDrop={() => { void handleDrop("customer", idx); }}
                                        onDragEnd={handleDragEnd}
                                        className={`border-b border-border/50 hover:bg-primary/5 transition-colors ${dragType === "customer" && dragOverIdx === idx ? "bg-primary/10 border-t-2 border-t-primary" : ""
                                            }`}
                                    >
                                        <td className="sticky left-0 z-10 bg-surface hover:bg-primary/5 px-1 py-1.5 text-text font-medium text-xs border-r border-border truncate max-w-[200px]" title={customer.name}>
                                            <div className="flex items-center gap-1 group">
                                                <span className="cursor-grab text-textMuted/30 group-hover:text-textMuted select-none" title="Drag to reorder">⋮⋮</span>
                                                {(customer.source === "invoice" || customer.source === "hybrid") && <span className="text-[9px] px-1 py-0.5 rounded bg-primary/20 text-accentIndigo font-bold flex-shrink-0" title={`${customer.open_invoices ?? 0} open AR item(s); open balance ${fmt(customer.total_open_balance)}`}>INV</span>}
                                                {customer.source === "hybrid" && <span className="text-[9px] px-1 py-0.5 rounded bg-success/20 text-accentEmerald font-bold flex-shrink-0" title={`Recurring run-rate from ${customer.run_rate_source === "open_items" ? "open AR" : "cash history"}`}>RUN</span>}
                                                {customer.source === "pattern" && <span className="text-[9px] px-1 py-0.5 rounded bg-surfaceAlt text-textMuted font-bold flex-shrink-0">PAT</span>}
                                                {customer.source === "manual" && <span className="text-[9px] px-1 py-0.5 rounded bg-accentPurple/20 text-accentPurple font-bold flex-shrink-0">MAN</span>}
                                                <span className="truncate flex-1">{customer.name}</span>
                                                <button
                                                    onClick={(e) => { e.stopPropagation(); void removeEntity(customer.row_key, customer.name, "customer"); }}
                                                    className="opacity-0 group-hover:opacity-100 text-danger/50 hover:text-danger text-xs px-1 transition-opacity"
                                                    title="Remove"
                                                >
                                                    ×
                                                </button>
                                            </div>
                                        </td>
                                        {renderCell(customer.row_key, "customer", -2, customer.effective_day, true, customer.name)}
                                        {renderCell(customer.row_key, "customer", -1, customer.effective_avg, true, customer.name)}
                                        {customer.amounts.map((amt, i) =>
                                            renderCell(customer.row_key, "customer", i, amt, true, customer.name)
                                        )}
                                    </tr>
                                ))}

                                {/* Add Customer button/input */}
                                <tr className="border-b border-border/50">
                                    <td colSpan={weeks + 3} className="sticky left-0 z-10 bg-surface px-4 py-1">
                                        {addingEntity === "customer" ? (
                                            <input
                                                ref={addEntityRef}
                                                type="text"
                                                placeholder="Customer name…"
                                                value={newEntityName}
                                                onChange={(e) => setNewEntityName(e.target.value)}
                                                onBlur={() => { setAddingEntity(null); setNewEntityName(""); }}
                                                onKeyDown={(e) => handleAddKeyDown(e, "customer")}
                                                className="w-64 px-2 py-1 text-xs bg-primary/10 border border-primary rounded text-text focus:outline-none"
                                            />
                                        ) : (
                                            <button
                                                onClick={() => setAddingEntity("customer")}
                                                className="text-xs text-primaryText/60 hover:text-primaryText transition-colors"
                                            >
                                                + Add Customer
                                            </button>
                                        )}
                                    </td>
                                </tr>

                                {/* Other Cash In — manual row */}
                                <tr className="border-b border-border/50 hover:bg-primary/5 transition-colors bg-surfaceAlt">
                                    <td className="sticky left-0 z-10 bg-surfaceAlt px-4 py-1.5 text-textMuted font-medium text-xs italic border-r border-border">
                                        Other Cash In
                                    </td>
                                    <td className="border-r border-border"></td>
                                    <td className="border-r border-border"></td>
                                    {data.other_cash_in.map((amt, i) =>
                                        renderCell("__other_in", "customer", i, amt)
                                    )}
                                </tr>

                                {/* Total Cash In */}
                                <tr className="bg-sectionHeader border-t-2 border-primary/50">
                                    <td className="sticky left-0 z-10 bg-sectionHeader px-4 py-2 font-bold text-primaryText text-xs border-r border-border">
                                        Total Cash In
                                    </td>
                                    <td className="border-r border-border"></td>
                                    <td className="border-r border-border"></td>
                                    {data.total_cash_in.map((t, i) => (
                                        <td key={i} className="px-2 py-2 text-right font-bold text-accentIndigo text-xs">
                                            {fmt(t)}
                                        </td>
                                    ))}
                                </tr>

                                {/* Spacer */}
                                <tr><td colSpan={weeks + 3} className="h-1 bg-border/30"></td></tr>

                                {/* ── VENDORS ── */}
                                <tr className="bg-accentAmber text-text">
                                    <td colSpan={weeks + 3} className="sticky left-0 px-4 py-2 font-bold text-xs tracking-wider uppercase">
                                        Vendor Obligations (Cash Out)
                                    </td>
                                </tr>

                                {data.vendors.map((vendor, idx) => (
                                    <tr
                                        key={vendor.row_key}
                                        data-testid={`forecast-row-${vendor.row_key}`}
                                        draggable
                                        onDragStart={() => handleDragStart("vendor", idx)}
                                        onDragOver={(e) => handleDragOver(e, idx)}
                                        onDrop={() => { void handleDrop("vendor", idx); }}
                                        onDragEnd={handleDragEnd}
                                        className={`border-b border-border/50 hover:bg-accentAmber/5 transition-colors ${dragType === "vendor" && dragOverIdx === idx ? "bg-accentAmber/10 border-t-2 border-t-accentAmber" : ""
                                            }`}
                                    >
                                        <td className="sticky left-0 z-10 bg-surface hover:bg-accentAmber/5 px-1 py-1.5 text-text font-medium text-xs border-r border-border truncate max-w-[200px]" title={vendor.name}>
                                            <div className="flex items-center gap-1 group">
                                                <span className="cursor-grab text-textMuted/30 group-hover:text-textMuted select-none" title="Drag to reorder">⋮⋮</span>
                                                {(vendor.source === "invoice" || vendor.source === "hybrid") && <span className="text-[9px] px-1 py-0.5 rounded bg-accentAmber/20 text-accentAmber font-bold flex-shrink-0" title={`${vendor.open_invoices ?? 0} open AP item(s); open balance ${fmt(vendor.total_open_balance)}`}>INV</span>}
                                                {vendor.source === "hybrid" && <span className="text-[9px] px-1 py-0.5 rounded bg-success/20 text-accentEmerald font-bold flex-shrink-0" title={`Recurring run-rate from ${vendor.run_rate_source === "open_items" ? "open AP" : "cash history"}`}>RUN</span>}
                                                {vendor.source === "pattern" && <span className="text-[9px] px-1 py-0.5 rounded bg-surfaceAlt text-textMuted font-bold flex-shrink-0">PAT</span>}
                                                {vendor.source === "manual" && <span className="text-[9px] px-1 py-0.5 rounded bg-accentPurple/20 text-accentPurple font-bold flex-shrink-0">MAN</span>}
                                                <span className="truncate flex-1">{vendor.name}</span>
                                                <button
                                                    onClick={(e) => { e.stopPropagation(); void removeEntity(vendor.row_key, vendor.name, "vendor"); }}
                                                    className="opacity-0 group-hover:opacity-100 text-danger/50 hover:text-danger text-xs px-1 transition-opacity"
                                                    title="Remove"
                                                >
                                                    ×
                                                </button>
                                            </div>
                                        </td>
                                        {renderCell(vendor.row_key, "vendor", -2, vendor.effective_day, true, vendor.name)}
                                        {renderCell(vendor.row_key, "vendor", -1, vendor.effective_avg, true, vendor.name)}
                                        {vendor.amounts.map((amt, i) =>
                                            renderCell(vendor.row_key, "vendor", i, amt, true, vendor.name)
                                        )}
                                    </tr>
                                ))}

                                {/* Add Vendor button/input */}
                                <tr className="border-b border-border/50">
                                    <td colSpan={weeks + 3} className="sticky left-0 z-10 bg-surface px-4 py-1">
                                        {addingEntity === "vendor" ? (
                                            <input
                                                ref={addEntityRef}
                                                type="text"
                                                placeholder="Vendor name…"
                                                value={newEntityName}
                                                onChange={(e) => setNewEntityName(e.target.value)}
                                                onBlur={() => { setAddingEntity(null); setNewEntityName(""); }}
                                                onKeyDown={(e) => handleAddKeyDown(e, "vendor")}
                                                className="w-64 px-2 py-1 text-xs bg-accentAmber/10 border border-accentAmber rounded text-text focus:outline-none"
                                            />
                                        ) : (
                                            <button
                                                onClick={() => setAddingEntity("vendor")}
                                                className="text-xs text-accentAmber/60 hover:text-accentAmber transition-colors"
                                            >
                                                + Add Vendor
                                            </button>
                                        )}
                                    </td>
                                </tr>

                                {/* Non Routine Expenses — manual row */}
                                <tr className="border-b border-border/50 hover:bg-accentAmber/5 transition-colors bg-surfaceAlt">
                                    <td className="sticky left-0 z-10 bg-surfaceAlt px-4 py-1.5 text-textMuted font-medium text-xs italic border-r border-border">
                                        Non Routine Expenses
                                    </td>
                                    <td className="border-r border-border"></td>
                                    <td className="border-r border-border"></td>
                                    {data.other_cash_out.map((amt, i) =>
                                        renderCell("__other_out", "vendor", i, amt)
                                    )}
                                </tr>

                                {/* Total Cash Out */}
                                <tr className="bg-sectionHeader border-t-2 border-accentAmber/50">
                                    <td className="sticky left-0 z-10 bg-sectionHeader px-4 py-2 font-bold text-accentAmber text-xs border-r border-border">
                                        Total Cash Out
                                    </td>
                                    <td className="border-r border-border"></td>
                                    <td className="border-r border-border"></td>
                                    {data.total_cash_out.map((t, i) => (
                                        <td key={i} className="px-2 py-2 text-right font-bold text-accentAmber text-xs">
                                            {fmt(t)}
                                        </td>
                                    ))}
                                </tr>

                                {/* Ending Bank Balance */}
                                <tr className="border-t-4 border-border">
                                    <td className="sticky left-0 z-10 bg-surfaceAlt text-text px-4 py-3 font-bold text-sm border-r border-border">
                                        Ending Bank Balance
                                    </td>
                                    <td className="bg-surfaceAlt border-r border-border"></td>
                                    <td className="bg-surfaceAlt border-r border-border"></td>
                                    {data.ending_balances.map((b, i) => {
                                        const anchorCol = (data.lookback ?? 0) > 0 ? (data.lookback ?? 0) - 1 : -1;
                                        const isAnchor = i === anchorCol;
                                        return (
                                            <td
                                                key={i}
                                                title={isAnchor ? "Control balance comes from the QBO balance sheet" : undefined}
                                                className={`px-2 py-3 text-right font-bold text-sm ${isAnchor ? "ring-1 ring-white/30 ring-inset" : ""} ${b == null
                                                    ? "bg-surfaceAlt text-textMuted"
                                                    : b < 0
                                                        ? "bg-dangerFill/90 text-white"
                                                        : "bg-success/15 text-successText"
                                                    }`}
                                            >
                                                {fmt(b)}
                                            </td>
                                        );
                                    })}
                                </tr>
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            {/* ── Categorization Modal ──────────────────────────────────────── */}
            {showSpreadModal && data && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
                    <div className="w-full max-w-xl rounded-xl border border-border bg-surface shadow-2xl">
                        <div className="flex items-center justify-between border-b border-border px-5 py-4">
                            <div>
                                <h2 className="text-lg font-bold text-text">Add Spread Schedule</h2>
                                <p className="text-xs text-textMuted mt-1">
                                    Layer an accrued or deferred balance into the Friday-ending forecast without overwriting the base row logic.
                                </p>
                            </div>
                            <button
                                onClick={() => {
                                    setShowSpreadModal(false);
                                    resetSpreadForm();
                                }}
                                className="text-textMuted hover:text-text text-lg transition"
                            >
                                ✕
                            </button>
                        </div>
                        <div className="grid grid-cols-2 gap-4 px-5 py-4">
                            <label className="col-span-2 text-xs text-textMuted">
                                Line Item
                                <input
                                    type="text"
                                    value={spreadForm.entity_name}
                                    onChange={(e) => setSpreadForm((prev) => ({ ...prev, entity_name: e.target.value }))}
                                    placeholder="Insurance accrual"
                                    className="mt-1 w-full rounded-lg border border-border bg-surfaceAlt px-3 py-2 text-sm text-text focus:outline-none focus:border-primary"
                                />
                            </label>
                            <label className="text-xs text-textMuted">
                                Direction
                                <select
                                    value={spreadForm.entity_type}
                                    onChange={(e) => setSpreadForm((prev) => ({ ...prev, entity_type: e.target.value as "customer" | "vendor" }))}
                                    className="mt-1 w-full rounded-lg border border-border bg-surfaceAlt px-3 py-2 text-sm text-text focus:outline-none focus:border-primary"
                                >
                                    <option value="vendor">Cash Out</option>
                                    <option value="customer">Cash In</option>
                                </select>
                            </label>
                            <label className="text-xs text-textMuted">
                                Total Amount
                                <input
                                    type="number"
                                    min="0"
                                    step="0.01"
                                    value={spreadForm.total_amount}
                                    onChange={(e) => setSpreadForm((prev) => ({ ...prev, total_amount: e.target.value }))}
                                    placeholder="24000"
                                    className="mt-1 w-full rounded-lg border border-border bg-surfaceAlt px-3 py-2 text-sm text-text focus:outline-none focus:border-primary"
                                />
                            </label>
                            <label className="text-xs text-textMuted">
                                Cadence
                                <select
                                    value={spreadForm.cadence}
                                    onChange={(e) => setSpreadForm((prev) => ({ ...prev, cadence: e.target.value as "weekly" | "monthly" }))}
                                    className="mt-1 w-full rounded-lg border border-border bg-surfaceAlt px-3 py-2 text-sm text-text focus:outline-none focus:border-primary"
                                >
                                    <option value="weekly">Weekly</option>
                                    <option value="monthly">Monthly</option>
                                </select>
                            </label>
                            <label className="text-xs text-textMuted">
                                Periods
                                <input
                                    type="number"
                                    min="1"
                                    max={spreadForm.cadence === "weekly" ? data.week_headers.length : 12}
                                    value={spreadForm.periods}
                                    onChange={(e) => setSpreadForm((prev) => ({ ...prev, periods: e.target.value }))}
                                    className="mt-1 w-full rounded-lg border border-border bg-surfaceAlt px-3 py-2 text-sm text-text focus:outline-none focus:border-primary"
                                />
                            </label>
                            <label className="text-xs text-textMuted">
                                Start Week
                                <select
                                    value={spreadForm.start_week_ending}
                                    onChange={(e) => setSpreadForm((prev) => ({ ...prev, start_week_ending: e.target.value }))}
                                    className="mt-1 w-full rounded-lg border border-border bg-surfaceAlt px-3 py-2 text-sm text-text focus:outline-none focus:border-primary"
                                >
                                    {data.week_headers.map((wk) => (
                                        <option key={wk} value={wk}>
                                            {new Date(wk + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" })}
                                        </option>
                                    ))}
                                </select>
                            </label>
                            <label className="text-xs text-textMuted">
                                Day of Month
                                <input
                                    type="number"
                                    min="1"
                                    max="28"
                                    value={spreadForm.day_of_month}
                                    onChange={(e) => setSpreadForm((prev) => ({ ...prev, day_of_month: e.target.value }))}
                                    className="mt-1 w-full rounded-lg border border-border bg-surfaceAlt px-3 py-2 text-sm text-text focus:outline-none focus:border-primary"
                                />
                            </label>
                            <label className="col-span-2 text-xs text-textMuted">
                                Note
                                <input
                                    type="text"
                                    value={spreadForm.note}
                                    onChange={(e) => setSpreadForm((prev) => ({ ...prev, note: e.target.value }))}
                                    placeholder="Spread deferred balance"
                                    className="mt-1 w-full rounded-lg border border-border bg-surfaceAlt px-3 py-2 text-sm text-text focus:outline-none focus:border-primary"
                                />
                            </label>
                        </div>
                        {spreadError && (
                            <div className="mx-5 mb-1 mt-0 rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
                                {spreadError}
                            </div>
                        )}
                        <div className="flex items-center justify-between border-t border-border px-5 py-4">
                            <p className="text-xs text-textMuted">
                                {spreadForm.cadence === "weekly"
                                    ? "Weekly spreads step forward one Friday bucket at a time."
                                    : "Monthly spreads land in the Friday-ending week that contains the selected day of month."}
                            </p>
                            <div className="flex gap-2">
                                <button
                                    onClick={() => {
                                        setShowSpreadModal(false);
                                        resetSpreadForm();
                                    }}
                                    className="px-3 py-2 rounded-lg border border-border text-sm text-textMuted hover:text-text transition"
                                >
                                    Cancel
                                </button>
                                <Button
                                    onClick={submitSpread}
                                    disabled={
                                        spreadSaving ||
                                        !spreadForm.entity_name.trim() ||
                                        !spreadForm.total_amount ||
                                        !spreadForm.periods
                                    }
                                    className="px-3 py-2 rounded-lg text-sm font-medium disabled:opacity-50"
                                >
                                    {spreadSaving ? "Saving..." : "Create Spread"}
                                </Button>
                            </div>
                        </div>
                    </div>
                </div>
            )}

            {uiIsCurrent && showCategorize && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
                    <div className="bg-surface border border-border rounded-xl shadow-2xl w-[900px] max-h-[80vh] flex flex-col">
                        {/* Header */}
                        <div className="flex items-center justify-between p-4 border-b border-border">
                            <div>
                                <h2 className="text-lg font-bold text-text">Categorize Bank Transactions</h2>
                                <p className="text-xs text-textMuted">Week ending {actualsWeek} • Map each transaction to a customer or vendor</p>
                                {actualsCashUniverse && (
                                    <p className={`mt-1 text-[11px] ${
                                        (actualsCashUniverse.unmatched_balance_sheet_row_count ?? 0) > 0 || (actualsCashUniverse.warnings?.length ?? 0) > 0
                                            ? "text-accentAmber"
                                            : "text-textMuted"
                                    }`}>
                                        Cash accounts included: {actualsCashUniverse.included_count ?? "—"}
                                        {" "}incl. {actualsCashUniverse.clearing_count ?? 0} clearing
                                        {(actualsCashUniverse.unmatched_balance_sheet_row_count ?? 0) > 0 && (
                                            <> • {actualsCashUniverse.unmatched_balance_sheet_row_count} Balance Sheet row(s) unmatched</>
                                        )}
                                        {(actualsCashUniverse.warnings?.length ?? 0) > 0 && (
                                            <> • {actualsCashUniverse.warnings?.[0]}</>
                                        )}
                                    </p>
                                )}
                            </div>
                            <div className="flex items-center gap-2">
                                <button
                                    onClick={() => { void resetActualsReview(); }}
                                    className="text-xs px-2 py-1 rounded border border-border text-textMuted hover:text-text transition"
                                    title="Re-pull from QBO"
                                >
                                    ↻ Re-pull
                                </button>
                                <button
                                    onClick={() => { void finishActualsReview(); }}
                                    className="text-xl text-textMuted hover:text-text px-2"
                                >×</button>
                            </div>
                        </div>

                        {/* Body */}
                        <div className="flex-1 overflow-y-auto p-4">
                            {actualsLoading ? (
                                <div className="text-center text-textMuted py-12">Loading transactions...</div>
                            ) : actuals.length === 0 ? (
                                <div className="text-center text-textMuted py-12">No bank transactions found for this week.</div>
                            ) : (
                                <table className="w-full text-sm">
                                    <thead>
                                        <tr className="text-textMuted text-xs border-b border-border">
                                            <th className="text-left py-2 px-2 w-20">Date</th>
                                            <th className="text-left py-2 px-2">Description</th>
                                            <th className="text-left py-2 px-2 w-28">Account</th>
                                            <th className="text-right py-2 px-2 w-24">Amount</th>
                                            <th className="text-center py-2 px-2 w-14">Dir</th>
                                            <th className="text-left py-2 px-2 w-44">Entity</th>
                                            <th className="text-center py-2 px-2 w-24">Status</th>
                                            <th className="text-center py-2 px-2 w-16">Actions</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {actuals.map((txn) => (
                                            <tr key={txn.id} className={`border-b border-border/50 hover:bg-primary/5 transition ${
                                                txn.category === "excluded" ? "opacity-40" : ""
                                            }`}>
                                                <td className="py-2 px-2 text-textMuted text-xs">{txn.txn_date}</td>
                                                <td className="py-2 px-2 text-text text-xs truncate max-w-[200px]" title={txn.description || undefined}>
                                                    {txn.is_billcom && <span className="text-[9px] px-1 py-0.5 rounded bg-danger/20 text-danger font-bold mr-1">BILL.COM</span>}
                                                    {txn.description || ""}
                                                </td>
                                                <td
                                                    className="py-2 px-2 text-textMuted text-xs truncate"
                                                    title={txn.qbo_account_id ? `${txn.account_name || ""} (${txn.qbo_account_id})` : txn.account_name || undefined}
                                                >
                                                    {txn.account_name}
                                                </td>
                                                <td className={`py-2 px-2 text-right font-medium text-xs ${txn.direction === "in" ? "text-accentEmerald" : "text-danger"}`}>
                                                    {txn.direction === "in" ? "+" : "-"}{fmt(txn.amount)}
                                                </td>
                                                <td className="py-2 px-2 text-center">
                                                    <span className={`text-[9px] px-1.5 py-0.5 rounded font-bold ${
                                                        txn.direction === "in" ? "bg-success/20 text-accentEmerald" : "bg-danger/20 text-danger"
                                                    }`}>
                                                        {txn.direction === "in" ? "IN" : "OUT"}
                                                    </span>
                                                </td>
                                                <td className="py-2 px-2 relative">
                                                    <div className="relative">
                                                        <input
                                                            type="text"
                                                            value={activeDropdown === txn.id ? dropdownSearch : (txn.entity_name || "")}
                                                            placeholder={txn.direction === "in" ? "Select customer..." : "Select vendor..."}
                                                            title="Entity name"
                                                            className={`w-full bg-transparent border rounded px-2 py-1 text-xs text-text focus:border-primary/50 focus:outline-none ${
                                                                txn.category === "categorized" ? "border-success/30" : "border-border/50"
                                                            }`}
                                                            onFocus={() => {
                                                                // Cancel any pending blur timeout so it doesn't kill this dropdown
                                                                if (dropdownDebounceRef.current) {
                                                                    clearTimeout(dropdownDebounceRef.current);
                                                                    dropdownDebounceRef.current = null;
                                                                }
                                                                setActiveDropdown(txn.id);
                                                                setDropdownSearch(txn.entity_name || "");
                                                                dropdownSearchRef.current = txn.entity_name || "";
                                                            }}
                                                            onChange={(e) => {
                                                                const val = e.target.value;
                                                                setDropdownSearch(val);
                                                                dropdownSearchRef.current = val;
                                                            }}
                                                            onBlur={() => {
                                                                // Delay closing so dropdown item clicks can register;
                                                                // tracked via ref so onFocus on another input can cancel it
                                                                const token = ownerRef.current;
                                                                dropdownDebounceRef.current = setTimeout(() => {
                                                                    if (!isCurrentOwner(token)) return;
                                                                    setActiveDropdown(null);
                                                                    dropdownDebounceRef.current = null;
                                                                }, 200);
                                                            }}
                                                        />
                                                        {activeDropdown === txn.id && (() => {
                                                            // Pull from forecast grid entities, not QBO master list
                                                            const gridEntities: EntityOption[] = txn.direction === "in"
                                                                ? (data?.customers || []).map(c => ({ name: c.name, id: c.name }))
                                                                : (data?.vendors || []).map(v => ({ name: v.name, id: v.name }));
                                                            const qboEntities: EntityOption[] = txn.direction === "in"
                                                                ? (entityList.customers || [])
                                                                : (entityList.vendors || []);
                                                            // Deduplicate by name and filter out non-entity names
                                                            const seen = new Set<string>();
                                                            const list = [...gridEntities, ...qboEntities].filter((e) => {
                                                                if (!e.name || seen.has(e.name)) return false;
                                                                // Bug 8: skip internal/system names
                                                                if (e.name.startsWith("__")) return false;
                                                                if (e.name.startsWith("Transfer →") || e.name.startsWith("Transfer ←")) return false;
                                                                seen.add(e.name);
                                                                return true;
                                                            });
                                                            const search = dropdownSearch.toLowerCase();
                                                            const filtered = list.filter((e) =>
                                                                e.name.toLowerCase().includes(search)
                                                            ).slice(0, 8);

                                                            // Special catch-all category for this direction
                                                            const specialOption = txn.direction === "in"
                                                                ? { id: "__other_in", name: "Other Cash In", type: "other_cash_in" }
                                                                : { id: "__other_out", name: "Non Routine Expenses", type: "non_routine" };
                                                            const showSpecial = specialOption.name.toLowerCase().includes(search);

                                                            if (filtered.length === 0 && !showSpecial) return null;

                                                            return (
                                                                <div className="absolute z-50 top-full left-0 right-0 mt-1 bg-surface border border-border rounded-lg shadow-xl max-h-[200px] overflow-y-auto">
                                                                    {/* Special catch-all option */}
                                                                    {showSpecial && (
                                                                        <button
                                                                            key={specialOption.id}
                                                                            className={`w-full text-left px-2 py-1.5 text-xs font-semibold transition truncate border-b border-border/30 ${
                                                                                txn.direction === "in"
                                                                                    ? "text-accentIndigo hover:bg-primary/10"
                                                                                    : "text-accentAmber hover:bg-accentAmber/10"
                                                                            }`}
                                                                            onMouseDown={(e) => {
                                                                                e.preventDefault();
                                                                                void categorizeActual(txn, specialOption.name, specialOption.type);
                                                                            }}
                                                                        >
                                                                            ▸ {specialOption.name}
                                                                        </button>
                                                                    )}
                                                                    {/* Regular entity options */}
                                                                    {filtered.map((entity) => (
                                                                        <button
                                                                            key={entity.id}
                                                                            className="w-full text-left px-2 py-1.5 text-xs text-text hover:bg-primary/10 transition truncate"
                                                                            onMouseDown={(e) => {
                                                                                e.preventDefault();
                                                                                const entityType = txn.direction === "in" ? "customer" : "vendor";
                                                                                void categorizeActual(txn, entity.name, entityType);
                                                                            }}
                                                                        >
                                                                            {entity.name}
                                                                        </button>
                                                                    ))}
                                                                </div>
                                                            );
                                                        })()}
                                                    </div>
                                                </td>
                                                <td className="py-2 px-2 text-center">
                                                    <span className={`text-[9px] px-1.5 py-0.5 rounded font-bold ${
                                                        txn.category === "categorized" ? "bg-success/20 text-accentEmerald" :
                                                        txn.category === "excluded" ? "bg-surfaceAlt text-textMuted" :
                                                        "bg-warning/20 text-accentAmber"
                                                    }`}>
                                                        {txn.category === "categorized" ? "✓" : txn.category === "excluded" ? "SKIP" : "PENDING"}
                                                    </span>
                                                </td>
                                                <td className="py-2 px-2 text-center">
                                                    <button
                                                        onClick={() => { void toggleActualExclusion(txn); }}
                                                        className="text-xs text-textMuted hover:text-text transition"
                                                        title={txn.category === "excluded" ? "Include" : "Exclude"}
                                                    >
                                                        {txn.category === "excluded" ? "↩" : "×"}
                                                    </button>
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            )}
                        </div>

                        {unmappedWarning && (
                            <div
                                // PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-5 AC-3 (F-4 — warn at source).
                                role="status"
                                data-testid="cff-unmapped-warning"
                                className="mx-4 mb-2 px-3 py-2 rounded-lg border border-warning/30 bg-warning/10 text-xs text-accentAmber flex items-start justify-between gap-3"
                            >
                                <span>
                                    <span className="font-semibold">{unmappedWarning.name}</span> won&rsquo;t appear
                                    in the cash forecast — it has no forecast row yet. Add one to keep this money in
                                    the projection.
                                </span>
                                <span className="flex items-center gap-2 flex-shrink-0">
                                    <button
                                        onClick={() => { void handleWarningAddRow(); }}
                                        disabled={warningAddBusy}
                                        className="px-2 py-0.5 rounded border border-warning/40 font-medium hover:bg-warning/20 transition disabled:opacity-50"
                                    >
                                        {warningAddBusy ? "Adding…" : "Add row"}
                                    </button>
                                    <button
                                        onClick={() => setUnmappedWarning(null)}
                                        className="text-accentAmber/70 hover:text-accentAmber transition"
                                        title="Dismiss"
                                        aria-label="Dismiss forecast-row warning"
                                    >
                                        <X className="h-3.5 w-3.5" />
                                    </button>
                                </span>
                            </div>
                        )}

                        {/* Footer */}
                        <div className="flex items-center justify-between p-4 border-t border-border">
                            <div className="text-xs text-textMuted">
                                {actuals.filter(t => t.category === "categorized").length} / {actuals.filter(t => t.category !== "excluded").length} categorized
                                {actuals.filter(t => t.is_billcom).length > 0 && (
                                    <span className="ml-2 text-danger">• {actuals.filter(t => t.is_billcom).length} Bill.com payment(s) need decomposition</span>
                                )}
                            </div>
                            <Button
                                onClick={() => { void finishActualsReview(); }}
                                className="px-4 py-2 rounded-lg text-sm font-medium transition"
                            >
                                Done — Update Forecast
                            </Button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
