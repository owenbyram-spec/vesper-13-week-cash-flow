// Shared API error extraction.
//
// The backend returns errors in several shapes:
//   - { detail: "plain string" }                      (most HTTPException raises)
//   - { detail: { error_code, user_message, ... } }   (structured errors, e.g. QBO 502)
//   - { detail: [ { loc, msg, type }, ... ] }          (FastAPI 422 validation)
//   - { message: "..." }                               (occasional)
//
// Rendering a non-string `detail` directly as a React child crashes the page
// ("Objects are not valid as a React child"); interpolating it yields
// "[object Object]". Always route error display through getApiErrorMessage so a
// human-readable STRING is guaranteed.

type StructuredDetail = {
    user_message?: unknown;
    message?: unknown;
    msg?: unknown;
    error_code?: unknown;
};

function nonEmptyString(value: unknown): string | null {
    return typeof value === "string" && value.trim() ? value : null;
}

function stringifyDetail(detail: unknown): string | null {
    if (detail == null) return null;
    if (typeof detail === "string") return nonEmptyString(detail);
    if (Array.isArray(detail)) {
        // FastAPI validation errors: [{ loc, msg, type }, ...]
        const msgs = detail
            .map((d) =>
                typeof d === "string"
                    ? d
                    : nonEmptyString((d as StructuredDetail)?.msg) ??
                      nonEmptyString((d as StructuredDetail)?.user_message) ??
                      nonEmptyString((d as StructuredDetail)?.message),
            )
            .filter((m): m is string => Boolean(m));
        return msgs.length ? msgs.join("; ") : null;
    }
    if (typeof detail === "object") {
        const d = detail as StructuredDetail;
        return (
            nonEmptyString(d.user_message) ??
            nonEmptyString(d.message) ??
            nonEmptyString(d.msg)
        );
    }
    return null;
}

/**
 * True when the backend signalled the QBO-not-connected PRECONDITION (HTTP 409
 * with detail.error_code === "qbo_not_connected"). This is NOT an auth failure —
 * pages should render a "Connect QuickBooks" CTA, and client.ts must never
 * re-auth/redirect on it.
 */
export function isQboNotConnectedError(error: unknown): boolean {
    const response = (error as { response?: { status?: number; data?: unknown } } | undefined)?.response;
    if (response?.status !== 409) return false;
    const detail = (response?.data as { detail?: { error_code?: unknown } } | undefined)?.detail;
    return !!detail && typeof detail === "object" && (detail as { error_code?: unknown }).error_code === "qbo_not_connected";
}

// PLAN-EXEMPT: QBO connection-health revocation-detection — the reconnect-required detector.
/**
 * True when the backend signalled that a QBO grant was REVOKED/invalidated on Intuit's
 * side (HTTP 409, detail.error_code === "qbo_reconnect_required"). Distinct from
 * isQboNotConnectedError (never-connected): here the company HAD a connection that died
 * out-of-band and needs a user RECONNECT. Like the not-connected 409, this is NOT an auth
 * failure — pages render a "Reconnect QuickBooks" CTA and client.ts must never re-auth on it.
 */
export function isQboReconnectRequiredError(error: unknown): boolean {
    const response = (error as { response?: { status?: number; data?: unknown } } | undefined)?.response;
    if (response?.status !== 409) return false;
    const detail = (response?.data as { detail?: { error_code?: unknown } } | undefined)?.detail;
    return !!detail && typeof detail === "object" && (detail as { error_code?: unknown }).error_code === "qbo_reconnect_required";
}

// PLAN-EXEMPT: QBO connection-health — either QBO connection precondition (connect OR reconnect).
/**
 * True for EITHER QBO connection precondition — never-connected (qbo_not_connected) or
 * revoked-needs-reconnect (qbo_reconnect_required). Use this to gate the QboNotConnected
 * CTA so a dead grant shows the reconnect prompt instead of a masked 400/500.
 */
export function isQboConnectionRequiredError(error: unknown): boolean {
    return isQboNotConnectedError(error) || isQboReconnectRequiredError(error);
}

/**
 * True when the backend blocked access because the COMPANY's billing is
 * suspended / past-due (HTTP 403, detail "This company is suspended"). This is
 * NOT a permission or cross-tenant failure — the user and tenant are fine; the
 * company's subscription needs attention. Pages should render a billing notice
 * (update payment), not a generic "access denied" error.
 */
export function isCompanySuspendedError(error: unknown): boolean {
    const response = (error as { response?: { status?: number; data?: unknown } } | undefined)?.response;
    if (response?.status !== 403) return false;
    const detail = (response?.data as { detail?: unknown } | undefined)?.detail;
    return typeof detail === "string" && detail.toLowerCase().includes("suspended");
}

/**
 * Coerce an arbitrary value (string, structured object, validation array) to a
 * human-readable string, or the fallback when nothing readable is found. Use
 * this when rendering payload fields of unknown shape (e.g. stream-event
 * content) so "[object Object]" can never reach the UI.
 */
export function toDisplayString(value: unknown, fallback: string): string {
    return stringifyDetail(value) ?? fallback;
}

const MAX_PLAIN_TEXT_LENGTH = 300;

/** Plain-text (non-JSON) error bodies: usable if short-ish and not an HTML page. */
function plainTextBody(data: unknown): string | null {
    if (typeof data !== "string") return null;
    const text = data.trim();
    if (!text || text.startsWith("<")) return null;
    return text.length > MAX_PLAIN_TEXT_LENGTH
        ? `${text.slice(0, MAX_PLAIN_TEXT_LENGTH)}…`
        : text;
}

/**
 * Resolve any thrown value (axios error, structured backend error, plain Error)
 * to a human-readable string. Never returns an object, so it is safe to render
 * directly or store in error state.
 */
export function getApiErrorMessage(
    error: unknown,
    fallback = "Something went wrong. Please try again.",
): string {
    const response = (error as { response?: { data?: unknown } } | undefined)?.response;
    const data = response?.data as
        | { detail?: unknown; message?: unknown; error?: unknown }
        | string
        | undefined;

    if (typeof data === "string") {
        const fromText = plainTextBody(data);
        if (fromText) return fromText;
    } else {
        const fromDetail = stringifyDetail(data?.detail);
        if (fromDetail) return fromDetail;

        const fromMessage = nonEmptyString(data?.message);
        if (fromMessage) return fromMessage;

        const fromError = nonEmptyString(data?.error);
        if (fromError) return fromError;
    }

    const errMessage = nonEmptyString((error as { message?: unknown } | undefined)?.message);
    if (errMessage) return errMessage;

    return fallback;
}
