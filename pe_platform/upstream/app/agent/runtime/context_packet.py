from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...config import settings
from ...models import AgentEpisode, ChatMessage, CompanyMemory, ExecutiveBriefing, QBOCompanyInfo, ToolFailureMemory
from .types import (
    AgentIntent,
    ContextPacket,
    EpisodicContextBlock,
    OutputMode,
    RiskLevel,
    SourceMetadata,
    ToolFailureBlock,
)
from .tool_registry import canonicalize_intent


IntentClassifier = Callable[[str], str]

# Sentinel role for the ONE rolling-summary item the history selector prepends
# (C7 AC-2). ``PromptBuilder.build_messages`` only forwards user/assistant roles,
# so this item never enters the message transcript; ``build_volatile_context``
# renders it as its own labeled volatile block via ``wrap_untrusted`` (it derives
# from conversation content and is therefore untrusted data, not instruction).
HISTORY_SUMMARY_ROLE = "conversation_summary"

# ── Deterministic structured digest (C7 AC-2) ─────────────────────────────────
# Line-level signal extractors. A history line matching ANY of these is kept
# VERBATIM (clipped, never paraphrased) in the rolling summary, so the classes
# the AC names survive structurally: decisions + reasoning, approvals/rejections
# + ProposedAction ids, standing constraints ("never do X", closed periods),
# open proposal ids, and cited figures + their source line. Deterministic by
# design — an in-turn LLM digest call would add hot-path latency (the S5 C3-D
# lesson), so none is made.
_SUMMARY_SIGNAL_RES: tuple[re.Pattern[str], ...] = (
    # approvals / rejections / confirmations
    re.compile(r"\b(approv\w*|reject\w*|declin\w*|confirm\w*)\b", re.IGNORECASE),
    # ProposedAction / proposal / action ids (open or referenced)
    re.compile(r"\b(?:ProposedAction|proposed\s+action|proposal|action)\s*(?:id\s*[:#]?\s*|#)\d+", re.IGNORECASE),
    # decisions + reasoning
    re.compile(r"\b(decid\w*|decision|chose|choos\w*|agreed|instead\s+of|because)\b", re.IGNORECASE),
    # standing constraints: closed periods, "never do X", locks, policy bounds
    re.compile(r"\b(never|must\s+not|do\s+not|don'?t|always|closed\s+period|locked|not\s+allowed|out\s+of\s+scope|policy)\b", re.IGNORECASE),
    # cited figures (money / percentages / large comma-grouped numbers) + source
    re.compile(r"[$€£]\s?\d|\b\d[\d,]*(?:\.\d+)?\s?%|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b"),
)

_SUMMARY_LINE_CLIP_CHARS = 300


def summarize_history_turns(turns: list[dict[str, str]]) -> str:
    """Compress older conversation turns into ONE deterministic structured digest.

    ``turns`` is oldest-first. Every line carrying a summary signal is kept
    verbatim (clipped to a per-line cap), tagged ``t<index> <speaker>:`` so
    ordering and attribution survive. Duplicate normalized lines are folded. The
    whole digest is bounded by ``settings.agent_history_message_clip_chars``.
    """
    if not turns:
        return ""
    kept: list[str] = []
    seen: set[str] = set()
    for index, turn in enumerate(turns, start=1):
        speaker = str(turn.get("role") or "unknown")  # SA-OK: bounded-loop dict lookup on already-selected history dicts, not a DB query
        text = str(turn.get("content") or "")  # SA-OK: bounded-loop dict lookup on already-selected history dicts, not a DB query
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if not any(pattern.search(line) for pattern in _SUMMARY_SIGNAL_RES):
                continue
            if len(line) > _SUMMARY_LINE_CLIP_CHARS:
                line = line[:_SUMMARY_LINE_CLIP_CHARS] + "…"
            normalized = line.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            kept.append(f"- t{index} {speaker}: {line}")
    header = (
        f"[Rolling summary of {len(turns)} earlier conversation turns, oldest first. "
        "Structured extract — decisions, approvals/rejections, proposal ids, standing "
        "constraints, and cited figures are preserved verbatim:]"
    )
    if not kept:
        return f"{header}\n- (no decision/approval/constraint/figure lines in the summarized turns)"
    digest = header + "\n" + "\n".join(kept)
    cap = max(200, settings.agent_history_message_clip_chars)
    if len(digest) > cap:
        # Head-keeping clip: the EARLIEST decisions/constraints (the ones no
        # longer verbatim anywhere else) survive; the newest summarized turns
        # are closest to the verbatim window and are re-derivable from it.
        digest = digest[:cap] + "\n…[summary clipped]"
    return digest


# ── Module context contract (LLM optimization roadmap #5) ────────────────────
# Declares which context sources each intent actually consumes. A focused intent
# should not pay to fetch + inject context it will never use — this trims prompt
# tokens AND removes the redundant per-request DB queries for those sources.
# Intents not listed fall back to ALL sources (the safe default), so general and
# reporting/analysis traffic is unchanged; only clearly non-financial-analysis
# intents are trimmed. Conversation history is always kept for continuity.
CTX_HISTORY = "conversation_history"
CTX_MEMORY = "company_memory"
CTX_FINANCIAL = "financial_context"
CTX_DOCUMENTS = "documents"
# LG-4 (Wave 3.3): run-start episodic recall. In ALL_CONTEXT_SOURCES (the safe
# default), so general/analysis/reporting traffic gets it; the deliberately-trimmed
# intents in MODULE_CONTEXT_CONTRACT (memory/documents/billing/admin) do not — same
# treatment as CTX_FINANCIAL.
CTX_EPISODIC = "episodic_context"
ALL_CONTEXT_SOURCES = frozenset({CTX_HISTORY, CTX_MEMORY, CTX_FINANCIAL, CTX_DOCUMENTS, CTX_EPISODIC})

MODULE_CONTEXT_CONTRACT: dict[str, frozenset] = {
    AgentIntent.MEMORY.value: frozenset({CTX_HISTORY, CTX_MEMORY}),
    AgentIntent.DOCUMENTS.value: frozenset({CTX_HISTORY, CTX_DOCUMENTS}),
    AgentIntent.BILLING.value: frozenset({CTX_HISTORY}),
    AgentIntent.ADMIN.value: frozenset({CTX_HISTORY}),
}


def context_sources_for_intent(intent: str) -> frozenset:
    """Return the set of context sources an intent is allowed to load."""
    return MODULE_CONTEXT_CONTRACT.get(intent, ALL_CONTEXT_SOURCES)


class ConversationHistorySelector:
    def select(self, db: Session, conversation_id: Optional[int], *, limit: int = 12) -> list[dict[str, Any]]:
        """Newest turns verbatim + ONE rolling summary of the older turns (C7 AC-2).

        The newest ``min(limit, agent_conversation_history_limit,
        agent_history_keep_verbatim)`` turns are returned verbatim (per-message
        clip promoted to ``agent_history_message_clip_chars``). Up to
        ``agent_history_summary_max_turns`` turns OLDER than that window are no
        longer dropped — they compress into one ``HISTORY_SUMMARY_ROLE`` item
        (prepended, oldest information first) via the deterministic
        ``summarize_history_turns`` digest.
        """
        if not conversation_id:
            return []
        verbatim_limit = max(
            1,
            min(limit, settings.agent_conversation_history_limit, settings.agent_history_keep_verbatim),
        )
        summary_window = max(0, settings.agent_history_summary_max_turns)
        message_clip = max(200, settings.agent_history_message_clip_chars)
        messages = (
            # CROSS-TENANT-OK: pre-existing HEAD query shape (only the limit arithmetic changed); conversation_id is resolved by the tenant-guarded runtime upstream and the live RLS backstop scopes the realm
            db.query(ChatMessage)
            .filter_by(conversation_id=conversation_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(verbatim_limit + summary_window)
            .all()
        )
        oldest_first = list(reversed(messages))
        older, verbatim_rows = oldest_first[:-verbatim_limit], oldest_first[-verbatim_limit:]
        selected = []
        if older:
            digest = summarize_history_turns(
                [
                    {"role": str(row.role or ""), "content": str(row.content or "")}
                    for row in older
                ]
            )
            if digest:
                selected.append(
                    {
                        "role": HISTORY_SUMMARY_ROLE,
                        "content": digest,
                        "turns_summarized": len(older),
                    }
                )
        for message in verbatim_rows:
            content = str(message.content or "")
            if len(content) > message_clip:
                content = content[:message_clip] + "...[truncated]"
            selected.append({"role": message.role, "content": content, "created_at": str(message.created_at)})
        return selected


class MemorySelector:
    def select(
        self,
        db: Session,
        company_id: str,
        *,
        organization_id: Optional[int] = None,
        organization_company_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        query = db.query(CompanyMemory).filter(CompanyMemory.company_id == company_id)
        if organization_id is not None:
            scoped_query = query.filter(CompanyMemory.organization_id == organization_id)
            if organization_company_id is not None:
                scoped_query = scoped_query.filter(CompanyMemory.organization_company_id == organization_company_id)
            memory = scoped_query.first()
            if not memory:
                memory = query.filter(
                    CompanyMemory.organization_id.is_(None),
                    CompanyMemory.organization_company_id.is_(None),
                ).first()
        else:
            memory = query.filter(CompanyMemory.organization_id.is_(None)).first()
        if not memory:
            return []
        selected = []
        if memory.company_profile:
            selected.append({"type": "company_profile", "content": memory.company_profile, "source": "CompanyMemory"})
        if memory.user_context:
            selected.append({"type": "user_context", "content": memory.user_context, "source": "CompanyMemory"})
        return selected


class FinancialContextSelector:
    def select(
        self,
        db: Session,
        company_id: str,
        *,
        organization_id: Optional[int] = None,
        organization_company_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        query = db.query(ExecutiveBriefing).filter(ExecutiveBriefing.company_id == company_id)
        if organization_id is not None:
            scoped_query = query.filter(ExecutiveBriefing.organization_id == organization_id)
            if organization_company_id is not None:
                scoped_query = scoped_query.filter(ExecutiveBriefing.organization_company_id == organization_company_id)
            briefing = scoped_query.order_by(ExecutiveBriefing.briefing_date.desc()).first()
            if not briefing:
                briefing = (
                    query.filter(
                        ExecutiveBriefing.organization_id.is_(None),
                        ExecutiveBriefing.organization_company_id.is_(None),
                    )
                    .order_by(ExecutiveBriefing.briefing_date.desc())
                    .first()
                )
        else:
            briefing = query.filter(ExecutiveBriefing.organization_id.is_(None)).order_by(ExecutiveBriefing.briefing_date.desc()).first()
        if not briefing or not briefing.narrative:
            return []
        return [
            {
                "type": "executive_briefing",
                "content": briefing.narrative,
                "period": str(briefing.briefing_date),
                "source": "ExecutiveBriefing",
                "pending_actions_count": briefing.pending_actions_count,
                "open_anomalies_count": briefing.open_anomalies_count,
            }
        ]


class DocumentContextSelector:
    def select(self, db: Session, company_id: str) -> list[dict[str, Any]]:
        # Keep this selector intentionally small. Detailed document text must be
        # fetched through scoped tools so it can be traced as a source.
        return []


class EpisodicMemorySelector:
    """Run-start similarity recall of past investigations (LG-4, Wave 3.3).

    Embeds the request text and cosine-ranks (``<=>``) the realm's non-expired
    ``AgentEpisode`` rows, returning the top-k as fenced, NON-AUTHORITATIVE blocks.
    They are rendered ONLY through ``wrap_untrusted`` (model-generated text — never
    authority: it never unlocks a tool, expands scope, confirms a proposal, or drives
    a write; every side effect stays proposal-gated, AWB-06). Realm + org scoped
    (app-layer primary; the live RLS backstop sits behind it). Dialect-guarded: the
    pgvector ``<=>`` path on Postgres, a numpy cosine fallback on the SQLite test
    dialect (mirrors ``app/assistant/context.py``)."""

    def select(
        self,
        db: Session,
        company_id: str,
        request_text: str,
        *,
        organization_id: int | None = None,
    ) -> list[EpisodicContextBlock]:
        query_text = (request_text or "").strip()
        if not query_text:
            return []
        from ...services.llm import analysis_llm

        q_emb = analysis_llm.get_embedding(query_text)
        if not q_emb:
            return []
        top_k = max(1, settings.agent_episodic_top_k)
        now = datetime.utcnow()
        # Tenant predicate is the PRIMARY wall (an unscoped <=> ranks across tenants;
        # under RLS it returns zero). Active (non-expired) rows only — the expires_at
        # decay horizon (D5-4).
        query = db.query(AgentEpisode).filter(AgentEpisode.company_id == company_id)
        if organization_id is not None:
            query = query.filter(AgentEpisode.organization_id == organization_id)
        query = query.filter(
            or_(AgentEpisode.expires_at.is_(None), AgentEpisode.expires_at > now)
        )
        if db.get_bind().dialect.name == "postgresql":
            rows = (
                query.filter(AgentEpisode.embedding_vector.isnot(None))
                .order_by(AgentEpisode.embedding_vector.cosine_distance(q_emb))
                .limit(top_k)
                .all()  # VECTOR-OK: index-in migrations/versions/b2f9d4a7c1e8_add_agent_episodes.py
            )
        else:
            rows = self._numpy_rank(query.all(), q_emb, top_k)
        # Own tight per-block clip (LG-4) so episodic recall cannot crowd out financial
        # context; the block is still fenced at render via _wrapped_blocks -> wrap_untrusted.
        clip = max(200, settings.agent_episodic_block_clip_chars)
        return [
            EpisodicContextBlock(
                type="past_investigation",
                content=row.content[:clip],
                source="AgentEpisode",
            )
            for row in rows
            if row.content
        ]

    @staticmethod
    def _numpy_rank(rows: list[AgentEpisode], q_emb: list[float], top_k: int) -> list[AgentEpisode]:
        import numpy as np  # VECTOR-OK: sqlite-test dialect numpy fallback (PG path is the <=> query above)

        q_vec = np.array(q_emb, dtype=float)
        norm_q = np.linalg.norm(q_vec)
        scored: list[tuple[float, AgentEpisode]] = []
        for row in rows:
            if not row.embedding_vector:
                continue
            k_vec = np.array(row.embedding_vector, dtype=float)
            norm_k = np.linalg.norm(k_vec)
            sim = float(np.dot(q_vec, k_vec) / (norm_q * norm_k)) if norm_q > 0 and norm_k > 0 else 0.0
            scored.append((sim, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [row for _, row in scored[:top_k]]


class ToolFailureSelector:
    """Always-on projection of a realm's OPEN tool-failure rows (LG-5, Wave 3.3).

    Projects ONLY the four CODE-CONTROLLED columns (tool_name, error_class,
    failure_count, last_seen) into the packet — it NEVER hydrates
    ``last_error_redacted`` (E1/E-D): the caveat renders in the TRUSTED, UNFENCED
    request block, so no model/exception-derived string may enter it. Filters to
    ``resolved_at IS NULL AND expires_at > now`` (a soft-resolved or expired row does
    not surface — a transient failure decays). Realm + org scoped (app-layer primary;
    the live RLS backstop sits behind it). NOT similarity-ranked and NOT intent-gated
    — it is a safety caveat; the render filters it to ``context.allowed_tools``."""

    def select(
        self,
        db: Session,
        company_id: str,
        *,
        organization_id: int | None = None,
    ) -> list[ToolFailureBlock]:
        now = datetime.utcnow()
        query = db.query(ToolFailureMemory).filter(ToolFailureMemory.company_id == company_id)
        if organization_id is not None:
            query = query.filter(ToolFailureMemory.organization_id == organization_id)
        # Only OPEN, unexpired failures (D5-4): a success soft-resolves (resolved_at set)
        # and the TTL horizon expires an abandoned broken tool with no beat.
        query = query.filter(
            ToolFailureMemory.resolved_at.is_(None),
            ToolFailureMemory.expires_at.isnot(None),
            ToolFailureMemory.expires_at > now,
        )
        rows = query.order_by(ToolFailureMemory.last_seen.desc()).all()
        return [
            ToolFailureBlock(
                tool_name=row.tool_name,
                error_class=row.error_class,
                failure_count=int(row.failure_count or 0),
                last_seen=row.last_seen.isoformat() if row.last_seen else "",
            )
            for row in rows
        ]


@dataclass
class PreparedContextSources:
    """Realm-scoped, request-stable context selections.

    These are the expensive, DB-derived (and potentially LLM-derived, via the
    intent classifier) pieces of a context packet that do NOT change between the
    provisional and final passes of ``AgentRuntime.prepare_session``. Computing
    them once and reusing them removes the duplicate request-path latency flagged
    by audit item S5. Every field here is realm-scoped by construction (the
    selectors that produce it already filter by company_id /
    organization_company_id), so reusing the bundle cannot leak context across
    realms — the bundle is rebuilt per request inside ``prepare_session``.
    """

    intent: str
    relevant_episodic_context: list[EpisodicContextBlock]
    known_tool_failures: list[ToolFailureBlock]
    # ``company_name`` uses the truthy fallback (used for the prompt / display);
    # ``source_label`` preserves the original packet label semantics (falls back
    # to company_id only when the company info row is entirely absent) so the
    # assembled packet is byte-identical to the pre-dedup behaviour.
    company_name: str
    source_label: str
    selected_conversation_history: list[dict[str, Any]]
    selected_company_memory: list[dict[str, Any]]
    relevant_financial_context: list[dict[str, Any]]
    relevant_documents_context: list[dict[str, Any]]


@dataclass
class ContextPacketBuilder:
    db: Session
    intent_classifier: Optional[IntentClassifier] = None
    conversation_selector: ConversationHistorySelector = ConversationHistorySelector()
    memory_selector: MemorySelector = MemorySelector()
    financial_selector: FinancialContextSelector = FinancialContextSelector()
    document_selector: DocumentContextSelector = DocumentContextSelector()
    episodic_selector: EpisodicMemorySelector = EpisodicMemorySelector()
    tool_failure_selector: ToolFailureSelector = ToolFailureSelector()

    def prepare_sources(
        self,
        *,
        request_text: str,
        company_id: str,
        conversation_id: Optional[int],
        organization_id: Optional[int] = None,
        organization_company_id: Optional[int] = None,
    ) -> PreparedContextSources:
        """Run the expensive, request-stable selection work exactly once.

        Used by ``AgentRuntime.prepare_session`` so the provisional pass (intent →
        model routing / tool selection) and the final pass (full packet assembly)
        share a single set of DB/embedding lookups instead of duplicating them.
        """
        intent = canonicalize_intent(
            self.intent_classifier(request_text) if self.intent_classifier else "general"
        )
        company_info = self.db.query(QBOCompanyInfo).filter_by(realm_id=company_id).first()
        company_name = (
            company_info.company_name if company_info and company_info.company_name else company_id
        )
        source_label = company_info.company_name if company_info else company_id
        # Only load the context sources this intent's contract declares (roadmap
        # #5). Sources outside the contract are skipped entirely — no DB query,
        # no prompt tokens. Unlisted intents load everything (safe default).
        allowed = context_sources_for_intent(str(intent or "general"))
        selected_conversation_history = (
            self.conversation_selector.select(self.db, conversation_id)
            if CTX_HISTORY in allowed
            else []
        )
        selected_company_memory = (
            self.memory_selector.select(
                self.db,
                company_id,
                organization_id=organization_id,
                organization_company_id=organization_company_id,
            )
            if CTX_MEMORY in allowed
            else []
        )
        relevant_financial_context = (
            self.financial_selector.select(
                self.db,
                company_id,
                organization_id=organization_id,
                organization_company_id=organization_company_id,
            )
            if CTX_FINANCIAL in allowed
            else []
        )
        relevant_documents_context = (
            self.document_selector.select(self.db, company_id) if CTX_DOCUMENTS in allowed else []
        )
        # LG-4 (Wave 3.3): embed the request text + cosine-rank past episodes. The
        # selector returns fenced, non-authoritative blocks (rendered via wrap_untrusted).
        relevant_episodic_context = (
            self.episodic_selector.select(
                self.db,
                company_id,
                request_text,
                organization_id=organization_id,
            )
            if CTX_EPISODIC in allowed
            else []
        )
        # LG-5 (Wave 3.3): always-on tool-failure caveat — the realm's OPEN failures
        # projected to code-controlled fields (NOT similarity-ranked, NOT intent-gated;
        # it is a safety caveat). The render filters it to context.allowed_tools.
        known_tool_failures = self.tool_failure_selector.select(
            self.db,
            company_id,
            organization_id=organization_id,
        )
        return PreparedContextSources(
            intent=str(intent or "general"),
            company_name=company_name,
            source_label=source_label,
            relevant_episodic_context=relevant_episodic_context,
            known_tool_failures=known_tool_failures,
            selected_conversation_history=selected_conversation_history,
            selected_company_memory=selected_company_memory,
            relevant_financial_context=relevant_financial_context,
            relevant_documents_context=relevant_documents_context,
        )

    def build(
        self,
        *,
        request_text: str,
        company_id: str,
        user: Any,
        user_role: str,
        conversation_id: Optional[int],
        output_mode: OutputMode,
        run_id: Optional[int] = None,
        tenant_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        organization_company_id: Optional[int] = None,
        model_routing_metadata: Optional[dict[str, Any]] = None,
        allowed_tools: Optional[list[str]] = None,
        forbidden_tools: Optional[list[dict[str, Any]]] = None,
        risk_level: RiskLevel = RiskLevel.INTERNAL_WRITE,
        confirmation_tokens: Optional[list[str]] = None,
        prepared_sources: Optional[PreparedContextSources] = None,
    ) -> ContextPacket:
        # Reuse already-computed, request-stable selections when the caller has
        # them (avoids the duplicate context build flagged by audit S5). When no
        # bundle is supplied (e.g. direct callers / tests), compute it inline so
        # behaviour is byte-identical to the pre-dedup path.
        sources = prepared_sources or self.prepare_sources(
            request_text=request_text,
            company_id=company_id,
            conversation_id=conversation_id,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
        )
        now = datetime.utcnow()
        source_metadata = [
            SourceMetadata(
                source_type="system",
                source_id=company_id,
                label=sources.source_label,
                period=now.strftime("%Y-%m"),
                metadata={"constructed_at": now.isoformat()},
            )
        ]
        return ContextPacket(
            run_id=run_id,
            conversation_id=conversation_id,
            user_id=getattr(user, "id", None),
            user_email=getattr(user, "email", None),
            tenant_id=tenant_id,
            company_id=company_id,
            qbo_realm_id=company_id,
            user_role=str(user_role or "VIEWER"),
            request_text=request_text[: settings.agent_max_request_chars],
            intent=str(sources.intent or "general"),
            risk_level=risk_level,
            output_mode=output_mode,
            selected_conversation_history=sources.selected_conversation_history,
            selected_company_memory=sources.selected_company_memory,
            relevant_financial_context=sources.relevant_financial_context,
            relevant_documents_context=sources.relevant_documents_context,
            relevant_episodic_context=sources.relevant_episodic_context,
            known_tool_failures=sources.known_tool_failures,
            allowed_tools=allowed_tools or [],
            forbidden_tools=forbidden_tools or [],
            assumptions=[],
            reporting_period=now.strftime("%Y-%m"),
            source_metadata=source_metadata,
            model_routing_metadata=model_routing_metadata or {},
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            confirmation_tokens=confirmation_tokens or [],
        )
