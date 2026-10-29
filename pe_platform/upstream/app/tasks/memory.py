# PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this worktree).
"""Memory maintenance tasks (AC-6: consolidation + decay + re-embed backfill).

Two per-realm workers, each pinned via ``tenant_scope_for_realm`` (NEVER the
cross-tenant bypass scope of scripts/reembed_company_knowledge.py — that scope is a
break-glass system-job affordance, not a beat-task pattern):

- ``reembed_company_knowledge_for_realm`` — embeds NULL-embedding rows in bounded
  batches (idempotent: the NULL-only filter makes re-runs skip embedded rows).
- ``consolidate_company_knowledge_for_realm`` — (a) merges near-duplicate ACTIVE
  memories (cosine similarity >= threshold, SAME scope + category only) via
  supersession (winner = newest accepted, else newest); (b) stamps an age-based
  ``expires_at`` on inferred-never-accepted facts so unconfirmed model inferences
  decay instead of accumulating forever. Taught/accepted facts are NEVER age-expired.

The parent fan-outs enumerate mapped realms under bypass and dispatch one
realm-scoped child each (AP-CEL-9, mirrors capture_all_metric_snapshots).
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from ..celery_app import celery_app
from ..db import SessionLocal
from ..models import AgentEpisode, AgentRun, AgentStep, CompanyKnowledge
from ..services.memory import FinancialMemoryService
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope, tenant_scope_for_realm

logger = logging.getLogger(__name__)

@celery_app.task
def refresh_monthly_financials_task(realm_id: str) -> None:
    db = SessionLocal()
    try:
        with tenant_scope_for_realm(db, realm_id):
            service = FinancialMemoryService(db)
            service.refresh_monthly_financials(realm_id)
    finally:
        db.close()

@celery_app.task
def update_vendor_profiles_task(realm_id: str) -> None:
    db = SessionLocal()
    try:
        with tenant_scope_for_realm(db, realm_id):
            service = FinancialMemoryService(db)
            service.update_vendor_profiles(realm_id)
    finally:
        db.close()


def _env_int(name: str, default: int) -> int:
    """Call-time env read (tunable without a redeploy); falls back on a bad value."""
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a possibly-naive column value (sqlite lane) to aware UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@celery_app.task(bind=True, max_retries=3, name="app.tasks.memory.reembed_company_knowledge_for_realm")
def reembed_company_knowledge_for_realm(self, realm_id: str) -> dict:
    """Embed NULL-embedding CompanyKnowledge rows for ONE realm, bounded batches.

    Idempotent by construction: only ``embedding_vector IS NULL`` rows are candidates,
    so an acks_late redelivery / re-run skips everything already embedded, and a row
    whose get_embedding() returns None stays NULL for the next run (AP-VEC-8 — a None
    is never persisted).
    """
    # IDEMPOTENT-OK: NULL-embedding-only candidate filter — a redelivery re-selects only
    # still-NULL rows; embedded rows are never re-written, so at-least-once delivery is safe.
    from ..services.llm import analysis_llm

    batch_limit = max(1, _env_int("MEMORY_REEMBED_BATCH_LIMIT", 200))
    commit_every = max(1, _env_int("MEMORY_REEMBED_COMMIT_BATCH", 50))
    embedded = 0
    skipped_no_embedding = 0
    db = SessionLocal()
    try:
        with tenant_scope_for_realm(db, realm_id):
            rows = (
                db.query(CompanyKnowledge)
                .filter(
                    CompanyKnowledge.company_id == realm_id,
                    CompanyKnowledge.embedding_vector.is_(None),
                    CompanyKnowledge.content.isnot(None),
                    CompanyKnowledge.content != "",
                )
                .order_by(CompanyKnowledge.id.asc())
                .limit(batch_limit)
                .all()
            )
            pending = 0
            for row in rows:
                # LONG-TASK-OK: bounded batch — at most MEMORY_REEMBED_BATCH_LIMIT (default
                # 200) embedding calls per run, far under the broker visibility_timeout.
                emb = analysis_llm.get_embedding(row.content)
                if not emb:
                    # None/empty embedding (no key / API error): leave the row NULL for a
                    # later run — never persist a None (AP-VEC-8).
                    skipped_no_embedding += 1
                    continue
                row.embedding_vector = emb
                embedded += 1
                pending += 1
                if pending >= commit_every:
                    db.commit()
                    pending = 0
            if pending:
                db.commit()
        logger.info(
            "[memory.reembed] realm=%s embedded=%s skipped=%s candidates=%s",
            realm_id, embedded, skipped_no_embedding, len(rows),
        )
        return {
            "status": "ok",
            "realm_id": realm_id,
            "embedded": embedded,
            "skipped_no_embedding": skipped_no_embedding,
            "candidates": len(rows),
        }
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error("[memory.reembed] realm=%s failed: %s", realm_id, redact_to_string(str(exc), max_length=400))
        # Capped exponential backoff (AP-CEL-4): 120s, 240s, 480s bounded at 600s.
        raise self.retry(exc=exc, countdown=min(120 * (2 ** self.request.retries), 600)) from exc
    finally:
        db.close()


def _active_embedded_rows(db, realm_id: str, now: datetime) -> list:
    return (
        db.query(CompanyKnowledge)
        .filter(
            CompanyKnowledge.company_id == realm_id,
            CompanyKnowledge.superseded_by_id.is_(None),
            or_(CompanyKnowledge.expires_at.is_(None), CompanyKnowledge.expires_at > now),
            CompanyKnowledge.embedding_vector.isnot(None),
        )
        .order_by(CompanyKnowledge.id.asc())
        .all()
    )


def _near_duplicate_pairs(db, rows: list, threshold: float) -> list[tuple[int, int]]:
    """(older_id, newer_id) pairs whose cosine similarity >= threshold, dialect-guarded."""
    pairs: list[tuple[int, int]] = []
    if db.get_bind().dialect.name == "postgresql":
        max_distance = 1.0 - threshold
        group_ids = [r.id for r in rows]
        if len(group_ids) >= 2:
            older = aliased(CompanyKnowledge)
            newer = aliased(CompanyKnowledge)
            join_rows = (
                db.query(older.id, newer.id)
                .join(newer, newer.id > older.id)
                .filter(
                    older.id.in_(group_ids),
                    newer.id.in_(group_ids),
                    # VECTOR-OK: ONE group-restricted self-join replaces the former per-row N+1 probe.
                    # The realm active set is unbounded, but this is the nightly background consolidation
                    # sweep, and the `<= max_distance` range predicate is NOT HNSW-accelerated (HNSW serves
                    # ORDER BY <=> LIMIT k), so a single self-join is the right shape. `newer.id > older.id`
                    # yields each unordered pair once; restricting BOTH sides to group_ids (= this
                    # (scope,category) group's rows) IS the AC-6 merge boundary — cross-scope pairs are
                    # never compared.
                    older.embedding_vector.cosine_distance(newer.embedding_vector) <= max_distance,
                )
                .all()
            )
            for older_id, newer_id in join_rows:
                pairs.append((older_id, newer_id))
    else:
        import numpy as np  # VECTOR-OK: sqlite-test dialect numpy fallback (PG path uses cosine_distance above)
        vectors = {}
        for r in rows:
            vec = np.array(r.embedding_vector, dtype=float)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vectors[r.id] = vec / norm
        ordered = [r for r in rows if r.id in vectors]
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                sim = float(np.dot(vectors[a.id], vectors[b.id]))
                if sim >= threshold:
                    pairs.append((a.id, b.id))
    return pairs


def _merge_clusters(rows: list, pairs: list[tuple[int, int]]) -> int:
    """Supersede near-duplicate clusters: winner = newest accepted, else newest."""
    parent = {r.id: r.id for r in rows}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (RATIFICATION-2026-07-13.md)
    by_root: dict[int, list] = {}
    for r in rows:
        by_root.setdefault(find(r.id), []).append(r)

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    superseded = 0
    for cluster in by_root.values():
        if len(cluster) < 2:
            continue
        accepted = [r for r in cluster if r.accepted_by]
        pool = accepted or cluster
        winner = max(pool, key=lambda r: (_as_utc(r.created_at) or epoch, r.id))
        for loser in cluster:
            if loser.id != winner.id and loser.superseded_by_id is None:
                # IDEMPOTENT-OK: supersession stamp — a re-run excludes superseded rows
                # from the candidate set, so the merge never re-applies.
                loser.superseded_by_id = winner.id
                superseded += 1
    # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (RATIFICATION-2026-07-13.md)
    return superseded


def _expire_stale_inferred(db, realm_id: str, now: datetime, expiry_days: int) -> int:
    """Stamp expires_at on inferred-NEVER-accepted facts (age decay). Taught (human
    source) and accepted rows are never touched — acceptance is a human signal."""
    candidates = (
        db.query(CompanyKnowledge)
        .filter(
            CompanyKnowledge.company_id == realm_id,
            CompanyKnowledge.source == "inferred",
            CompanyKnowledge.accepted_by.is_(None),
            CompanyKnowledge.superseded_by_id.is_(None),
            CompanyKnowledge.expires_at.is_(None),
        )
        .all()
    )
    stamped = 0
    for row in candidates:
        created = _as_utc(row.created_at) or now
        # IDEMPOTENT-OK: only expires_at-IS-NULL rows are candidates — a re-run skips
        # every already-stamped row, so at-least-once delivery is safe.
        row.expires_at = created + timedelta(days=expiry_days)
        stamped += 1
    return stamped


@celery_app.task(bind=True, max_retries=2, name="app.tasks.memory.consolidate_company_knowledge_for_realm")
def consolidate_company_knowledge_for_realm(self, realm_id: str) -> dict:
    """Deterministic memory consolidation + decay for ONE realm (decision 9).

    Cosine >= MEMORY_CONSOLIDATION_COSINE_THRESHOLD (default 0.95, read at call time)
    same-scope+category merge via supersession; age-based expires_at (default 180d,
    MEMORY_INFERRED_EXPIRY_DAYS) for inferred-never-accepted facts. No LLM judging.
    """
    threshold = _env_float("MEMORY_CONSOLIDATION_COSINE_THRESHOLD", 0.95)
    expiry_days = max(1, _env_int("MEMORY_INFERRED_EXPIRY_DAYS", 180))
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        with tenant_scope_for_realm(db, realm_id):
            rows = _active_embedded_rows(db, realm_id, now)
            groups: dict[tuple[str, str], list] = {}
            for r in rows:
                key = (r.scope or "company", r.category or "")
                groups.setdefault(key, []).append(r)
            superseded = 0
            for group_rows in groups.values():
                if len(group_rows) < 2:
                    continue
                # Cross-scope / cross-category pairs are NEVER compared — the grouping
                # key is the merge boundary (AC-6: cross-scope pairs NOT merged).
                pairs = _near_duplicate_pairs(db, group_rows, threshold)
                if pairs:
                    superseded += _merge_clusters(group_rows, pairs)
            expired = _expire_stale_inferred(db, realm_id, now, expiry_days)
            db.commit()
        logger.info(
            "[memory.consolidate] realm=%s superseded=%s expired_stamped=%s active_embedded=%s",
            realm_id, superseded, expired, len(rows),
        )
        return {
            "status": "ok",
            "realm_id": realm_id,
            "superseded": superseded,
            "expired_stamped": expired,
            "active_embedded": len(rows),
        }
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error("[memory.consolidate] realm=%s failed: %s", realm_id, redact_to_string(str(exc), max_length=400))
        raise self.retry(exc=exc, countdown=min(180 * (2 ** self.request.retries), 600)) from exc
    finally:
        db.close()


# PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2/AC-6 (marker
# RATIFICATION-2026-07-13.md) — orchestrator wave-gate fix: the tests/test_celery.py
# registration guard resolves enqueue symbols statically and cannot see through a
# task-object alias, so the concrete `.delay` calls move into each parent task.
def _realms_for_dispatch(label: str) -> list[str]:
    """Enumerate mapped, non-suspended realms under bypass for an AP-CEL-9 fan-out
    (mirrors capture_all_metric_snapshots). The `.delay` calls live in each parent
    task on the CONCRETE task name — the registration guard demands it."""
    from ..models import OrganizationCompany
    from ..services.tenant_companies import company_is_suspended

    db = SessionLocal()
    try:
        with tenant_scope(bypass=True):  # CROSS-TENANT-OK: vetted sweep over OrganizationCompany (RLS-excluded mapping); each child re-pins its own realm scope
            companies = (
                db.query(OrganizationCompany)
                .filter(OrganizationCompany.realm_id.isnot(None))
                .all()
            )
            realms = sorted({
                company.realm_id
                for company in companies
                if company.realm_id and not company_is_suspended(db, organization_company=company)
            })
    finally:
        db.close()

    logger.info("[memory.%s] realms=%s", label, len(realms))
    return realms


# PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2/AC-6 — concrete-name
# `.delay` loops per parent so the static registration guard resolves the enqueues.
@celery_app.task(name="app.tasks.memory.reembed_all_company_knowledge")
def reembed_all_company_knowledge() -> dict:
    """Nightly fan-out: re-embed backfill, one realm-scoped child per mapped realm."""
    # IDEMPOTENT-OK: pure dispatcher — children carry the NULL-only idempotency guard.
    realms = _realms_for_dispatch("reembed-dispatch")
    for realm_id in realms:
        reembed_company_knowledge_for_realm.delay(realm_id)
    return {"dispatched": len(realms)}


@celery_app.task(name="app.tasks.memory.consolidate_all_company_knowledge")
def consolidate_all_company_knowledge() -> dict:
    """Nightly fan-out: consolidation + decay, one realm-scoped child per mapped realm."""
    # IDEMPOTENT-OK: pure dispatcher — children stamp supersession/expiry idempotently.
    realms = _realms_for_dispatch("consolidate-dispatch")
    for realm_id in realms:
        consolidate_company_knowledge_for_realm.delay(realm_id)
    return {"dispatched": len(realms)}


# ── LG-4 episodic distillation (plan 20260714-wave3-3-episodic-toolfail-memory C1) ──
# PLAN-EXEMPT: chunk 1 of ratified plan 20260714-wave3-3-episodic-toolfail-memory
# (marker RATIFICATION-2026-07-14.md on disk; active-plan.json sidecar absent in this worktree).
# Terminal-SUCCESS run states worth an episode. FAILED / cancelled / expired /
# requires_manual_reconciliation runs are NEVER distilled into a "concluded" episode
# (D5-6: refuse over fabricate — a run with no honest conclusion produces no memory).
_DISTILL_SUCCESS_STATES = frozenset({"succeeded", "completed", "partially_completed"})


def _succeeded_tool_names(db, run_id: int) -> list[str]:
    """The distinct succeeded tool-call names for a run (the honest 'evidence' leg),
    in call order. A step whose status is not 'succeeded' is NOT claimed as evidence
    (D5-6) — the distiller never over-claims a tool that failed/blocked."""
    rows = (
        # CROSS-TENANT-OK: agent_steps is RLS-excluded (no tenant column); this reads the
        # steps of run_id, whose run was already realm-filtered in _distill_candidate_runs
        # under the pinned tenant_scope_for_realm — the run FK is the scoping wall.
        db.query(AgentStep.tool_name)
        .filter(
            AgentStep.agent_run_id == run_id,
            AgentStep.step_type == "tool_call",
            AgentStep.status == "succeeded",
            AgentStep.tool_name.isnot(None),
        )
        .order_by(AgentStep.id.asc())
        .all()
    )
    names: list[str] = []
    for (name,) in rows:
        if name and name not in names:  # SA-OK: bounded-loop de-dup over already-fetched step-name rows, not a DB query
            names.append(name)
    return names


def _assemble_episode_content(run, tool_names: list[str]) -> str | None:
    """TEMPLATE-assemble the honest episode paragraph from the run's OWN trace — NO
    LLM pass, no embellishment (D5-6). Returns None when the run concluded nothing
    (empty output_summary) OR made no non-trivial succeeded tool call: a run that
    concluded nothing produces NO episode (refuse over fabricate)."""
    concluded = (run.output_summary or "").strip()
    if not concluded or not tool_names:
        return None
    investigated = (run.input_summary or "").strip() or "(no recorded objective)"
    trigger = run.trigger_type or "chat"
    when = run.finished_at.strftime("%Y-%m-%d") if run.finished_at else ""
    stamp = f"{trigger}, {when}" if when else trigger
    return (
        f"Run #{run.id} ({stamp}) investigated: {investigated}\n"
        f"Concluded: {concluded}\n"
        f"Evidence: called {', '.join(tool_names)}."
    )


def _distill_candidate_runs(db, realm_id: str, limit: int) -> list:
    """Terminal-success, finished, org-resolved runs for this realm with NO episode
    yet (NOT IN over this realm's non-null agent_episodes.agent_run_id). This is the
    reembed NULL-filter idempotency shape: a redelivery re-selects only still-
    undistilled runs, so at-least-once delivery is safe. organization_id must be
    present because agent_episodes.organization_id is NOT NULL (no NULL-org orphan)."""
    distilled = (
        db.query(AgentEpisode.agent_run_id)
        .filter(AgentEpisode.company_id == realm_id, AgentEpisode.agent_run_id.isnot(None))
    )
    return (
        db.query(AgentRun)
        .filter(
            AgentRun.company_id == realm_id,
            AgentRun.status.in_(_DISTILL_SUCCESS_STATES),
            AgentRun.finished_at.isnot(None),
            AgentRun.organization_id.isnot(None),
            AgentRun.id.notin_(distilled),
        )
        .order_by(AgentRun.id.asc())
        .limit(limit)
        .all()
    )


@celery_app.task(bind=True, max_retries=3, name="app.tasks.memory.distill_agent_episodes_for_realm")
def distill_agent_episodes_for_realm(self, realm_id: str) -> dict:
    """Distill terminal-SUCCESS AgentRuns for ONE realm into episodic memory (LG-4).

    Content is TEMPLATE-assembled from the run's OWN trace (input/output summary +
    succeeded tool-step names) — NO second LLM pass, no fabrication. Embed-at-write,
    null-guarded (never persist a None embedding, AP-VEC-8). Own SessionLocal in
    try/finally; realm scope pinned via tenant_scope_for_realm.
    """
    # IDEMPOTENT-OK: not-yet-distilled (NOT IN agent_episodes.agent_run_id) candidate
    # filter + UNIQUE(agent_run_id) — a redelivery re-selects only undistilled runs and
    # a concurrent double-fire hits the unique key (rolled back), so no dup episode.
    from ..services.llm import analysis_llm

    batch_limit = max(1, _env_int("MEMORY_DISTILL_BATCH_LIMIT", 100))
    expiry_days = max(1, _env_int("AGENT_EPISODIC_EXPIRY_DAYS", 90))
    now = datetime.now(timezone.utc)
    written = 0
    skipped_no_conclusion = 0
    skipped_no_embedding = 0
    db = SessionLocal()
    try:
        with tenant_scope_for_realm(db, realm_id):
            candidates = _distill_candidate_runs(db, realm_id, batch_limit)
            for run in candidates:
                # LONG-TASK-OK: bounded batch — at most MEMORY_DISTILL_BATCH_LIMIT
                # (default 100) runs per realm per run, one embedding call each.
                content = _assemble_episode_content(run, _succeeded_tool_names(db, run.id))
                if content is None:
                    # Concluded nothing / no succeeded tool step → refuse over fabricate.
                    skipped_no_conclusion += 1
                    continue
                emb = analysis_llm.get_embedding(content)
                if not emb:
                    # None/empty embedding → never persist a None (AP-VEC-8); the run
                    # stays undistilled (still a NOT-IN candidate) for a later run.
                    skipped_no_embedding += 1
                    continue
                episode = AgentEpisode(
                    organization_id=run.organization_id,
                    organization_company_id=run.organization_company_id,
                    company_id=realm_id,
                    agent_run_id=run.id,
                    trigger_type=run.trigger_type,
                    content=content,
                    embedding_vector=emb,
                    expires_at=now + timedelta(days=expiry_days),
                )
                db.add(episode)
                try:
                    # IDEMPOTENT-OK: UNIQUE(agent_run_id) — a concurrent fire that already
                    # distilled this run raises IntegrityError; the redelivery is a no-op.
                    db.commit()
                    written += 1
                except IntegrityError:
                    db.rollback()
        logger.info(
            "[memory.distill] realm=%s written=%s skipped_no_conclusion=%s skipped_no_embedding=%s candidates=%s",
            realm_id, written, skipped_no_conclusion, skipped_no_embedding, len(candidates),
        )
        return {
            "status": "ok",
            "realm_id": realm_id,
            "written": written,
            "skipped_no_conclusion": skipped_no_conclusion,
            "skipped_no_embedding": skipped_no_embedding,
            "candidates": len(candidates),
        }
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error("[memory.distill] realm=%s failed: %s", realm_id, redact_to_string(str(exc), max_length=400))
        # Capped exponential backoff (AP-CEL-4): 120s, 240s, 480s bounded at 600s.
        raise self.retry(exc=exc, countdown=min(120 * (2 ** self.request.retries), 600)) from exc
    finally:
        db.close()


# PLAN-EXEMPT: chunk 1 of ratified plan 20260714-wave3-3-episodic-toolfail-memory —
# concrete-name `.delay` loop per parent so the static registration guard resolves the enqueue.
@celery_app.task(name="app.tasks.memory.distill_all_agent_episodes")
def distill_all_agent_episodes() -> dict:
    """Nightly fan-out: distill terminal-success runs into episodic memory, one
    realm-scoped child per mapped realm (LG-4)."""
    # IDEMPOTENT-OK: pure dispatcher — children carry the NOT-IN + UNIQUE(agent_run_id) dedup.
    realms = _realms_for_dispatch("distill-dispatch")
    for realm_id in realms:
        distill_agent_episodes_for_realm.delay(realm_id)
    return {"dispatched": len(realms)}
