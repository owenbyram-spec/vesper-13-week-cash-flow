"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { useAppStore } from "@/lib/store";
import { apiClient } from "@/lib/api/client";
import {
    assistantErrorCode,
    assistantMessageForErrorCode,
    formatAssistantError,
    streamAssistantMessage,
    type AssistantMessageMetadata,
} from "@/lib/api/assistant";
import { toDisplayString } from "@/lib/api/errors";
import { Bot, X, Send, Loader2, User, ShieldCheck, Database, CalendarRange, Sigma, PauseCircle, Leaf, GraduationCap } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { usePathname } from "next/navigation";
import { Button } from "@/components/ui/button";

function parseMessage(text: string) {
    const qrRegex = /\[QUICK_REPLIES:\s*(.+?)\]/i;
    const match = text.match(qrRegex);
    let quickReplies: string[] = [];
    let cleanText = text;

    if (match && match[1]) {
        cleanText = text.replace(qrRegex, '').trim();
        const parts = match[1].split('|').map(s => s.trim().replace(/^"/, '').replace(/"$/, ''));
        quickReplies = parts;
    }
    return { cleanText, quickReplies };
}

interface Message {
    id: string;
    role: "user" | "assistant";
    content: string;
    metadata?: MessageMetadata | null;
    verificationPending?: boolean;
    verificationIncomplete?: boolean;
    /** Typed backend error code (app/assistant/errors.py) when this bubble is
     *  an error reply — drives the distinct usage-limit rendering. */
    errorCode?: string | null;
}

// Backend 429 codes that mean "responses are paused by a workspace limit" —
// rendered as a distinct paused state, not a generic error bubble.
const USAGE_LIMIT_ERROR_CODES = new Set([
    "assistant_budget_reached",
    "assistant_quota_exceeded",
]);

type MessageMetadata = AssistantMessageMetadata;

// Soft budget pressure stepped this answer to a cheaper model tier
// (app/agent/performance.py downgrade_route stamps `budget_downgrade:` into the
// route reason; the `_noop` suffix means pressure applied but no cheaper tier
// existed, so nothing actually changed).
function isEconomyMode(metadata?: MessageMetadata | null): boolean {
    const reason = metadata?.model_route?.reason ?? "";
    return reason.includes("budget_downgrade") && !reason.endsWith("_noop");
}

interface AgentRun {
    id: number;
    status: string;
    trigger_type: string;
    model_name?: string | null;
    created_at?: string | null;
}

function formatExplanationTime(value?: string | null) {
    if (!value || !Number.isFinite(Date.parse(value))) return "Unknown";
    return new Date(value).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
    });
}

function hasExplanation(metadata?: MessageMetadata | null) {
    if (!metadata) return false;
    return Boolean(
        metadata.verification_schema_version != null ||
        metadata.critical_warnings?.length ||
        metadata.observation != null ||
        metadata.confidence != null ||
        metadata.source_refs?.length ||
        metadata.periods?.length ||
        metadata.formula_notes?.length ||
        metadata.caveats?.length ||
        metadata.model?.name ||
        metadata.usage?.total_tokens ||
        metadata.corrections_applied
    );
}

function verificationLabel(metadata?: MessageMetadata | null, pending?: boolean, incomplete?: boolean): string {
    if (incomplete) return "Verification incomplete";
    if (pending || metadata?.verification?.provisional) return "Provisional — final checks pending";
    if (metadata?.verification_schema_version !== 1) return "Legacy response — verification unknown";
    const labels = {
        not_checked: "Not checked",
        no_numeric_claims: "No numeric claims checked",
        limited: "Limited numeric checks",
        conflict: "Numeric conflict — review required",
        checked_numeric_claims: "Numeric claims checked",
    };
    return metadata.verification ? labels[metadata.verification.status] ?? "Verification unknown" : "Verification unknown";
}

export function AssistantPanel() {
    const { activeCompanyId } = useAppStore();
    const pathname = usePathname();
    const [open, setOpen] = useState(pathname === "/assistant");
    const [messages, setMessages] = useState<Message[]>([
        {
            id: "welcome",
            role: "assistant",
            content:
                "I can retrieve authorized financial context, explain results, and draft proposals. Available sources and approval requirements depend on your company and the task.",
        },
    ]);
    const [input, setInput] = useState("");
    const [isLoading, setIsLoading] = useState(false);
    const [statusText, setStatusText] = useState("Analyzing...");
    const [historyLoaded, setHistoryLoaded] = useState(false);
    const [recentRuns, setRecentRuns] = useState<AgentRun[]>([]);
    const messagesEndRef = useRef<HTMLDivElement>(null);

    const scrollToBottom = () => {
        messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    };

    useEffect(() => {
        scrollToBottom();
    }, [messages, isLoading]);

    // Load chat history when panel opens
    useEffect(() => {
        if (!activeCompanyId || historyLoaded || !open) return;
        apiClient
            .get(`/assistant/history?realm_id=${activeCompanyId}`)
            .then((res) => {
                const hist = res.data as { role: string; content: string }[];
                if (hist.length > 0) {
                    setMessages([
                        {
                            id: "welcome",
                            role: "assistant",
                            content:
                                "I can retrieve authorized financial context, explain results, and draft proposals. Available sources and approval requirements depend on your company and the task.",
                        },
                        ...hist.map((m, i) => ({
                            id: `hist-${i}`,
                            role: m.role as "user" | "assistant",
                            content: m.content,
                            metadata: (m as { metadata?: MessageMetadata | null }).metadata ?? null,
                        })),
                    ]);
                }
                setHistoryLoaded(true);
            })
            .catch(() => setHistoryLoaded(true));
    }, [activeCompanyId, historyLoaded, open]);

    const loadRecentRuns = useCallback(() => {
        if (!activeCompanyId || !open) return;
        apiClient
            .get(`/agent/runs?company_id=${activeCompanyId}&limit=3`)
            .then((res) => setRecentRuns(res.data as AgentRun[]))
            .catch(() => setRecentRuns([]));
    }, [activeCompanyId, open]);

    useEffect(() => {
        loadRecentRuns();
    }, [loadRecentRuns]);

    useEffect(() => {
        if (pathname === "/assistant") {
            setOpen(true);
        }
    }, [pathname]);

    const handleSend = useCallback(
        async (text?: string) => {
            const msg = (text || input).trim();
            if (!msg || !activeCompanyId) return;
            const userMessage: Message = {
                id: Date.now().toString(),
                role: "user",
                content: msg,
            };
            setMessages((prev) => [...prev, userMessage]);
            setInput("");
            setIsLoading(true);
            setStatusText("Thinking...");

            // Create a placeholder for the streaming response
            const assistantId = (Date.now() + 1).toString();
            setMessages((prev) => [
                ...prev,
                { id: assistantId, role: "assistant", content: "", verificationPending: true },
            ]);

            try {
                await streamAssistantMessage(
                    {
                        message: msg,
                        realm_id: activeCompanyId,
                    },
                    (event) => {
                        if (event.type === "text") {
                            setMessages((prev) =>
                                prev.map((m) =>
                                    m.id === assistantId
                                        ? { ...m, content: m.content + toDisplayString(event.content, "") }
                                        : m
                                )
                            );
                        } else if (event.type === "explanation") {
                            setMessages((prev) =>
                                prev.map((m) =>
                                    m.id === assistantId
                                        ? { ...m, metadata: event.content as MessageMetadata }
                                        : m
                                )
                            );
                        } else if (event.type === "done") {
                            setMessages((prev) => prev.map((m) => m.id === assistantId
                                ? { ...m, verificationPending: false }
                                : m));
                        } else if (event.type === "status") {
                            setStatusText(toDisplayString(event.content, "Working..."));
                        } else if (event.type === "tool") {
                            setMessages((prev) =>
                                prev.map((m) =>
                                    m.id === assistantId
                                        ? { ...m, content: m.content + `\n[${event.tool || "tool"}] ${event.result || toDisplayString(event.content, "")}\n` }
                                        : m
                                )
                            );
                        } else if (event.type === "error") {
                            // SSE error frames carry the typed envelope
                            // (error_code / user_message) — prefer the friendly
                            // code-specific copy over a generic failure line.
                            const friendly =
                                assistantMessageForErrorCode(event.error_code)
                                ?? (typeof event.user_message === "string" && event.user_message
                                    ? event.user_message
                                    : toDisplayString(event.content, "The assistant hit an error while generating a response."));
                            setMessages((prev) =>
                                prev.map((m) =>
                                    m.id === assistantId
                                        ? { ...m, content: m.content ? `${m.content}\n\n${friendly}` : friendly, errorCode: event.error_code ?? null, verificationPending: false, verificationIncomplete: true }
                                        : m
                                )
                            );
                        }
                    }
                );
            } catch (error: unknown) {
                const message = formatAssistantError(error);
                const errorCode = assistantErrorCode(error) ?? null;
                setMessages((prev) =>
                    prev.map((m) =>
                        m.id === assistantId
                            ? {
                                ...m,
                                content: m.content ? `${m.content}

${message}` : message,
                                errorCode,
                                verificationPending: false,
                                verificationIncomplete: true,
                            }
                            : m
                    )
                );
            } finally {
                setIsLoading(false);
                setStatusText("Analyzing...");
                loadRecentRuns();
            }
        },
        [input, activeCompanyId, loadRecentRuns]
    );

    // Don't render if no company is connected
    if (!activeCompanyId) return null;

    const latestRun = recentRuns[0];
    const latestRunLabel = latestRun
        ? `${latestRun.status.replace("_", " ")} #${latestRun.id}`
        : "Ready";

    // ─── Collapsed FAB ───
    if (!open) {
        return (
            <Button
                onClick={() => setOpen(true)}
                className="fixed bottom-6 right-6 z-50 h-14 w-14 rounded-full shadow-lg shadow-primary/30 flex items-center justify-center hover:scale-110 transition-transform"
                aria-label="Open Vesper Bot"
                title="Open Vesper Bot"
            >
                <Bot className="h-6 w-6" />
            </Button>
        );
    }

    // ─── Expanded Panel ───
    return (
        <div className="w-[380px] h-full flex-shrink-0 flex flex-col bg-surface border-l border-border relative z-40">
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-surface">
                <div className="flex items-center gap-2">
                    <div className="h-7 w-7 rounded-full bg-primary/20 flex items-center justify-center">
                        <Bot className="h-4 w-4 text-primaryText" />
                    </div>
                    <div>
                        <h3 className="text-sm font-semibold text-text">Vesper Bot</h3>
                        <p className="text-[9px] text-textMuted truncate max-w-[170px]">{latestRunLabel}</p>
                    </div>
                </div>
                <button
                    onClick={() => setOpen(false)}
                    className="p-1.5 rounded-lg text-textMuted hover:text-text hover:bg-surfaceAlt transition-colors"
                    aria-label="Minimize Vesper Bot"
                    title="Minimize"
                >
                    <X className="h-4 w-4" />
                </button>
            </div>

            {/* Messages */}
            <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
                {messages.map((message, index) => {
                    const { cleanText, quickReplies } = parseMessage(message.content);
                    const isLast = index === messages.length - 1;
                    const explanation = message.metadata;
                    // Exemplar provenance: >0 only when the backend reports past
                    // human corrections were actually injected into THIS answer.
                    // Historic messages without the field render no chip.
                    const correctionsApplied = explanation?.corrections_applied ?? 0;
                    const isUsageLimitNotice =
                        message.role === "assistant" &&
                        message.errorCode != null &&
                        USAGE_LIMIT_ERROR_CODES.has(message.errorCode);
                    return (
                        <div
                            key={message.id}
                            className={`flex gap-2.5 ${message.role === "user" ? "flex-row-reverse" : ""}`}
                        >
                            <div
                                className={`shrink-0 h-6 w-6 rounded-full flex items-center justify-center mt-0.5 ${message.role === "assistant"
                                    ? "bg-primary/15 text-primaryText"
                                    : "bg-surfaceAlt text-textMuted"
                                    }`}
                            >
                                {message.role === "assistant" ? (
                                    <Bot className="h-3.5 w-3.5" />
                                ) : (
                                    <User className="h-3.5 w-3.5" />
                                )}
                            </div>
                            <div className="flex flex-col gap-2 max-w-[85%] min-w-0">
                                <div
                                    className={`rounded-2xl px-3.5 py-2 text-[13px] leading-relaxed w-full overflow-hidden ${message.role === "assistant"
                                        ? `${isUsageLimitNotice ? "bg-warning/10 border border-warning/40" : "bg-surfaceAlt border border-border/40"} text-text prose prose-sm max-w-none break-words prose-headings:text-text prose-p:text-text prose-strong:text-text prose-li:text-text prose-ol:text-text prose-ul:text-text prose-code:text-text prose-pre:text-text prose-th:text-text prose-td:text-text prose-a:text-primaryText marker:text-textMuted dark:prose-invert [&>p]:whitespace-pre-wrap [&>pre]:overflow-x-auto [&>table]:overflow-x-auto`
                                        : "bg-primary text-white font-medium break-words"
                                        }`}
                                >
                                    {isUsageLimitNotice && (
                                        <div className="flex items-center gap-1.5 text-[11px] font-semibold text-warningText not-prose mb-1">
                                            <PauseCircle className="h-3.5 w-3.5" />
                                            Responses paused — workspace AI limit
                                        </div>
                                    )}
                                    {message.role === "assistant" ? (
                                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{cleanText}</ReactMarkdown>
                                    ) : (
                                        <div className="whitespace-pre-wrap break-words">{cleanText}</div>
                                    )}
                                </div>
                                
                                {message.role === "assistant" && quickReplies.length > 0 && !isLoading && isLast && (
                                    <div className="flex flex-wrap gap-2 mt-1">
                                        {quickReplies.map((qr, idx) => (
                                            <button
                                                key={idx}
                                                onClick={() => handleSend(qr)}
                                                className="text-[11px] px-3 py-1.5 rounded-full border border-primary/40 bg-primary/5 text-primaryText hover:bg-primary/10 transition-colors text-left"
                                            >
                                                {qr}
                                            </button>
                                        ))}
                                    </div>
                                )}

                                {message.role === "assistant" && (hasExplanation(explanation) || message.verificationPending || message.verificationIncomplete) && (
                                    <div className="rounded-xl border border-border/60 bg-surface px-3 py-2 text-[11px] text-textMuted space-y-2">
                                        <div className="flex flex-wrap items-center gap-2">
                                            <span className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-1">
                                                <ShieldCheck className="h-3 w-3" />
                                                {verificationLabel(explanation, message.verificationPending, message.verificationIncomplete)}
                                            </span>
                                            <span className="inline-flex items-center gap-1">
                                                <CalendarRange className="h-3 w-3" />
                                                Source freshness: {formatExplanationTime(explanation?.verification_schema_version === 1 ? explanation.data_freshness_at : null)}
                                            </span>
                                            {explanation?.verification_schema_version === 1 && explanation.generated_at && (
                                                <span>Generated: {formatExplanationTime(explanation.generated_at)}</span>
                                            )}
                                            {explanation?.model?.name && (
                                                <span
                                                    title={`Provider: ${explanation.model.provider ?? "unknown"}`
                                                        + (explanation.model.lane ? ` · lane: ${explanation.model.lane}` : "")
                                                        + (explanation.model.escalated ? " · escalated mid-answer" : "")}
                                                    className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-1 font-mono"
                                                >
                                                    <Bot className="h-3 w-3" />
                                                    {explanation.model.name}
                                                    {explanation.model.escalated ? " ↑" : ""}
                                                </span>
                                            )}
                                            {isEconomyMode(explanation) && (
                                                <span
                                                    title="Budget pressure routed this answer to a cheaper model tier"
                                                    className="inline-flex items-center gap-1 rounded-full border border-warning/40 bg-warning/10 px-2 py-1 text-warningText"
                                                >
                                                    <Leaf className="h-3 w-3" />
                                                    economy mode
                                                </span>
                                            )}
                                            {correctionsApplied > 0 && (
                                                <span
                                                    title="This answer used examples from corrections your team made previously."
                                                    className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-1"
                                                >
                                                    <GraduationCap className="h-3 w-3" />
                                                    Learned from {correctionsApplied} past correction{correctionsApplied === 1 ? "" : "s"}
                                                </span>
                                            )}
                                            {explanation?.usage?.total_tokens ? (
                                                <span
                                                    title={`in ${explanation.usage.input_tokens ?? 0} · out ${explanation.usage.output_tokens ?? 0}`
                                                        + (explanation.usage.estimated_cost_usd != null
                                                            ? ` · ~$${explanation.usage.estimated_cost_usd.toFixed(4)}`
                                                            : "")}
                                                    className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-1"
                                                >
                                                    {explanation.usage.total_tokens.toLocaleString()} tokens
                                                </span>
                                            ) : null}
                                        </div>

                                        {explanation?.periods?.length ? (
                                            <div className="flex flex-wrap gap-1.5">
                                                {explanation.periods.slice(0, 4).map((period) => (
                                                    <span key={period} className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-1">
                                                        <CalendarRange className="h-3 w-3" />
                                                        {period}
                                                    </span>
                                                ))}
                                            </div>
                                        ) : null}

                                        {explanation?.source_refs?.length ? (
                                            <div className="space-y-1">
                                                <div className="flex items-center gap-1 text-text">
                                                    <Database className="h-3 w-3" />
                                                    Sources
                                                </div>
                                                {explanation.source_refs.slice(0, 3).map((source, idx) => (
                                                    <div key={`${source.tool || source.label || "source"}-${idx}`} className="leading-relaxed">
                                                        <span className="text-text">{source.label || source.tool || "Source"}</span>
                                                        {source.period ? <span> | {source.period}</span> : null}
                                                    </div>
                                                ))}
                                            </div>
                                        ) : null}

                                        {explanation?.formula_notes?.length ? (
                                            <div className="space-y-1">
                                                <div className="flex items-center gap-1 text-text">
                                                    <Sigma className="h-3 w-3" />
                                                    Calculation
                                                </div>
                                                {explanation.formula_notes.slice(0, 2).map((note, idx) => (
                                                    <div key={`${note}-${idx}`} className="leading-relaxed">
                                                        {note}
                                                    </div>
                                                ))}
                                            </div>
                                        ) : null}

                                        {explanation?.critical_warnings?.map((warning, idx) => (
                                            <div key={`critical-${idx}`} role="alert" className="text-warningText">{warning}</div>
                                        ))}
                                        {explanation?.observation?.status === "incomplete" && (
                                            <div role="alert" className="text-warningText">
                                                Observation incomplete. {explanation.observation.reasons.join(" ")}
                                            </div>
                                        )}
                                        {explanation?.caveats?.length ? (
                                            <div className="space-y-1">
                                                {explanation.caveats.map((caveat, idx) => (
                                                    <div key={`${caveat}-${idx}`} className="leading-relaxed text-warningText">
                                                        {caveat}
                                                    </div>
                                                ))}
                                            </div>
                                        ) : null}
                                    </div>
                                )}
                            </div>
                        </div>
                    );
                })}

                {isLoading && (
                    <div className="flex gap-2.5">
                        <div className="shrink-0 h-6 w-6 rounded-full bg-primary/15 text-primaryText flex items-center justify-center mt-0.5">
                            <Bot className="h-3.5 w-3.5" />
                        </div>
                        <div className="rounded-2xl px-3.5 py-2 bg-surfaceAlt text-text border border-border/40 flex items-center gap-2">
                            <Loader2 className="h-3.5 w-3.5 animate-spin text-primaryText" />
                            <span className="text-xs text-textMuted">{statusText}</span>
                        </div>
                    </div>
                )}
                <div ref={messagesEndRef} />
            </div>

            {/* Input */}
            <div className="shrink-0 p-3 border-t border-border bg-surface">
                <form
                    onSubmit={(e) => {
                        e.preventDefault();
                        handleSend();
                    }}
                    className="relative flex items-center"
                >
                    <input
                        type="text"
                        value={input}
                        onChange={(e) => setInput(e.target.value)}
                        placeholder="Ask about budgets, covenants, deals…"
                        className="w-full bg-surfaceAlt border border-border rounded-full pl-4 pr-10 py-2.5 text-xs text-text focus:outline-none focus:border-primary transition-all"
                        disabled={isLoading}
                    />
                    <button
                        type="submit"
                        disabled={!input.trim() || isLoading}
                        className="absolute right-1.5 p-1.5 rounded-full bg-primary/10 text-primaryText hover:bg-primary/20 disabled:opacity-40 transition-colors"
                    >
                        <Send className="h-3.5 w-3.5" />
                    </button>
                </form>
            </div>
        </div>
    );
}
