import { apiClient } from './client';
import { currentWorkspaceScope, isCurrentScope, registerWorkspaceStream, StaleWorkspaceError } from '../workspace-scope';
// PLAN-EXEMPT: iOS/iPadOS login fix — assistant SSE stream goes same-origin (/bff). Steven-authorized.
import { BackendConfigurationError, buildApiHeaders, getApiBaseUrl } from './client';

export interface AssistantModelInfo {
    name?: string | null;
    provider?: string | null;
    lane?: string | null;
    escalated?: boolean | null;
}

// The route the backend chose for this answer (app/assistant/main.py explanation
// metadata `model_route`). `reason` carries `budget_downgrade:*` when soft
// budget pressure stepped the model a tier cheaper — the panel's "economy mode"
// hint keys off it.
export interface AssistantModelRouteInfo {
    provider?: string | null;
    model?: string | null;
    lane?: string | null;
    reason?: string | null;
}

export interface AssistantUsageInfo {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
    estimated_cost_usd?: number | null;
}

export type AssistantVerificationStatus = 'not_checked' | 'no_numeric_claims' | 'limited' | 'conflict' | 'checked_numeric_claims';

export interface AssistantMessageMetadata {
    verification_schema_version?: number;
    verification?: { status: AssistantVerificationStatus; scope: 'numeric_claims'; provisional: boolean };
    critical_warnings?: string[];
    generated_at?: string | null;
    observation?: { status: 'complete' | 'incomplete'; reasons: string[] };
    /** Historical only. Unversioned confidence is not a verification result. */
    confidence?: number | null;
    source_refs?: Array<{ label?: string; tool?: string; period?: string }>;
    periods?: string[];
    formula_notes?: string[];
    caveats?: string[];
    data_freshness_at?: string | null;
    model?: AssistantModelInfo | null;
    model_route?: AssistantModelRouteInfo | null;
    usage?: AssistantUsageInfo | null;
    // How many past human corrections were actually injected as few-shot
    // exemplars into this answer (app/assistant/main.py `corrections_applied`).
    // 0 = none; absent on historic messages that predate the field.
    corrections_applied?: number | null;
}

export interface AssistantStreamPayload {
    message: string;
    realm_id: string;
    confirmation_token?: string;
    confirmation_tokens?: string[];
}

export interface AssistantStreamEvent {
    type: string;
    content?: unknown;
    tool?: string;
    result?: string;
    // SSE `error` frames (app/assistant/main.py stream error path) carry the
    // typed error envelope alongside `type: "error"`.
    error_code?: string;
    user_message?: string;
    retryable?: boolean;
}

type AssistantEventHandler = (event: AssistantStreamEvent) => void;

export class AssistantApiError extends Error {
    status?: number;
    errorCode?: string;
    userMessage: string;
    transportFailure: boolean;

    constructor(message: string, options: { status?: number; errorCode?: string; userMessage?: string; transportFailure?: boolean } = {}) {
        super(message);
        this.name = 'AssistantApiError';
        this.status = options.status;
        this.errorCode = options.errorCode;
        this.transportFailure = Boolean(options.transportFailure);
        this.userMessage = options.userMessage
            || assistantMessageForErrorCode(options.errorCode)
            || assistantMessageForStatus(options.status);
    }
}

// Friendly copy for the backend's typed assistant error codes
// (app/assistant/errors.py) — 429s especially must not collapse into a generic
// "something went wrong": budget/quota are admin-actionable, provider limits
// are retryable.
const ASSISTANT_ERROR_CODE_MESSAGES: Record<string, string> = {
    assistant_budget_unavailable:
        'The AI budget could not be checked. No model request was started. Please try again shortly.',
    assistant_budget_reached:
        'Monthly AI budget reached — assistant responses are paused until the budget resets. An admin can raise the budget in Settings.',
    assistant_quota_exceeded:
        'This workspace hit its monthly AI token quota — assistant responses are paused until the quota resets. An admin can raise it in Settings.',
    assistant_provider_rate_limited:
        'The AI service is temporarily busy. Give it a few seconds and try again.',
    assistant_company_suspended:
        "This company's assistant access is suspended. Contact your administrator to restore it.",
    assistant_duplicate_run:
        'A request is already in progress. Wait for it to finish, then ask again.',
    assistant_request_too_large:
        'Your message is too long for the assistant. Shorten it and try again.',
    assistant_session_rate_limited:
        'The assistant is temporarily rate limited. Please try again shortly.',
};

export function assistantMessageForErrorCode(errorCode?: string | null): string | null {
    if (!errorCode) return null;
    return ASSISTANT_ERROR_CODE_MESSAGES[errorCode] ?? null;
}

export function assistantMessageForStatus(status?: number) {
    if (status === 401) {
        return 'Your Vesper Bot session expired. Please sign in again, then retry.';
    }
    if (status === 403) {
        return 'Vesper Bot blocked this assistant request for security or permission reasons. Check your company access and refresh the page.';
    }
    if (status === 404) {
        return 'The assistant API route is unavailable in this deployment. Check that the backend service is current.';
    }
    if (status === 429) {
        return 'The assistant hit a usage limit (rate, quota, or budget). Try again shortly — if it persists, an admin can review AI usage in Settings.';
    }
    if (status && status >= 500) {
        return 'The assistant service is having trouble right now. Try again in a moment.';
    }
    return 'Vesper Bot cannot reach the API service. Check that the backend is running and that NEXT_PUBLIC_BACKEND_URL points to the API.';
}

// Error envelope shape shared by the stream's non-2xx JSON body and the axios
// fallback path (app/services/errors.py safe_error_detail).
interface AssistantErrorDetail {
    error_code?: string;
    user_message?: string;
}

export function formatAssistantError(error: unknown) {
    if (error instanceof BackendConfigurationError) {
        return error.userMessage;
    }
    if (error instanceof AssistantApiError) {
        return error.userMessage;
    }
    const response = (error as { response?: { status?: number; data?: { detail?: AssistantErrorDetail | string } } } | null)?.response;
    const detail = response?.data?.detail;
    if (typeof detail === 'string') return detail.trim() || assistantMessageForStatus(response?.status);
    const codeMessage = assistantMessageForErrorCode(detail?.error_code)
        ?? detail?.user_message;
    if (codeMessage) {
        return codeMessage;
    }
    if (response?.status) {
        return assistantMessageForStatus(response.status);
    }
    return assistantMessageForStatus();
}

export function assistantErrorCode(error: unknown): string | undefined {
    if (error instanceof AssistantApiError) return error.errorCode;
    const detail = (error as { response?: { data?: { detail?: AssistantErrorDetail } } } | null)?.response?.data?.detail;
    return detail?.error_code;
}

function isTransportError(error: unknown) {
    if (error instanceof AssistantApiError) return error.transportFailure;
    if (error instanceof TypeError) return true;
    const code = (error as { code?: string } | null)?.code;
    return code === 'ERR_NETWORK' || code === 'ECONNABORTED';
}

function parseSseEvent(rawEvent: string): AssistantStreamEvent | null {
    const dataLines = rawEvent
        .split(/\r?\n/)
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.replace(/^data:\s?/, ''));

    if (!dataLines.length) return null;
    const data = dataLines.join('\n').trim();
    if (!data || data === '[DONE]') return { type: 'done' };

    try {
        const parsed = JSON.parse(data);
        return typeof parsed === 'object' && parsed !== null
            ? parsed as AssistantStreamEvent
            : { type: 'text', content: String(parsed) };
    } catch {
        return { type: 'text', content: data };
    }
}

async function streamAssistantTransport(payload: AssistantStreamPayload, onEvent: AssistantEventHandler, signal?: AbortSignal) {
    let response: Response;
    try {
        // PLAN-EXEMPT: iOS login fix — same-origin (/bff) so the session cookie is sent first-party.
        response = await fetch(`${getApiBaseUrl()}/assistant/stream`, {
            method: 'POST',
            signal,
            credentials: 'include',
            headers: buildApiHeaders('POST', {
                Accept: 'text/event-stream',
                'Content-Type': 'application/json',
            }),
            body: JSON.stringify(payload),
        });
    } catch (error) {
        if (error instanceof BackendConfigurationError || signal?.aborted) throw error;
        throw new AssistantApiError('Assistant stream transport failed', { transportFailure: true });
    }

    if (!response.ok) {
        // The backend rejects prep failures with a typed error envelope
        // ({ detail: { error_code, user_message } }) — read it so a 429
        // budget/quota stop surfaces as its specific state, not a generic one.
        let errorCode: string | undefined;
        let userMessage: string | undefined;
        try {
            const body = await response.json() as { detail?: AssistantErrorDetail | string };
            const detail = body?.detail;
            if (typeof detail === 'string') {
                userMessage = detail;
            } else {
                errorCode = detail?.error_code;
                userMessage = assistantMessageForErrorCode(errorCode) ?? detail?.user_message;
            }
        } catch {
            // Non-JSON error body — fall through to the status-based message.
        }
        throw new AssistantApiError(`Assistant stream failed with HTTP ${response.status}`, {
            status: response.status,
            errorCode,
            userMessage,
        });
    }
    if (!response.body) {
        throw new AssistantApiError('Assistant stream response did not include a body', { transportFailure: true });
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let sawDone = false;
    const deliver = (event: AssistantStreamEvent) => {
        if (event.type === 'done') sawDone = true;
        onEvent(event);
    };

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split(/\r?\n\r?\n/);
        buffer = events.pop() || '';

        for (const rawEvent of events) {
            const event = parseSseEvent(rawEvent);
            if (event) deliver(event);
        }
    }

    buffer += decoder.decode();
    const event = parseSseEvent(buffer);
    if (event) deliver(event);
    if (!sawDone) {
        throw new AssistantApiError('Assistant stream ended before completion', {
            userMessage: 'The response was interrupted before final checks completed. Review any partial answer before using it.',
        });
    }
}

async function queryAssistantFallback(payload: AssistantStreamPayload, onEvent: AssistantEventHandler) {
    const { data } = await apiClient.post('/assistant/query', payload);
    if (data?.answer) {
        onEvent({ type: 'text', content: data.answer });
    }
    if (data?.explanation) {
        onEvent({ type: 'explanation', content: data.explanation });
    }
    onEvent({ type: 'done' });
}

export async function streamAssistantMessage(payload: AssistantStreamPayload, onEvent: AssistantEventHandler) {
    const scope = currentWorkspaceScope();
    if (!scope || scope.realmId !== payload.realm_id) throw new StaleWorkspaceError();
    const controller = new AbortController();
    const unregister = registerWorkspaceStream(controller);
    let receivedEvent = false;
    const deliver = (event: AssistantStreamEvent) => {
        if (!isCurrentScope(scope) || controller.signal.aborted) throw new StaleWorkspaceError();
        receivedEvent = true;
        onEvent(event);
    };
    try {
        try {
            await streamAssistantTransport(payload, deliver, controller.signal);
        } catch (error) {
            if (!isCurrentScope(scope) || controller.signal.aborted) throw new StaleWorkspaceError();
            if (!receivedEvent && isTransportError(error)) {
                await queryAssistantFallback(payload, deliver);
                return;
            }
            throw error;
        }
    } finally {
        unregister();
    }
}
