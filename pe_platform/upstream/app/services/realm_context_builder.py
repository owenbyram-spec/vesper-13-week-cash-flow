"""
app/services/realm_context_builder.py
─────────────────────────────────────
RealmContextBuilder — the precompute substrate for audit item S6.

Builds / refreshes a single isolated ``RealmContextProfile`` plus the
precomputed onboarding-recommendation ``RealmContextArtifact`` rows for one
QBO realm, FROM the same approved sources used by ``build_onboarding_plan``.

CORE INVARIANT (realm isolation):
    Every row written or read here is scoped by the full triple
    (organization_id / organization_company_id / qbo_realm_id). The builder
    resolves the triple up front and refuses to run if the realm is not mapped
    to an organization + organization_company. It never reads or writes another
    realm's profile or artifacts: all queries filter on the resolved triple, and
    the profile is upserted against the UNIQUE (org, company, realm) constraint.

SAFETY:
    Only summarized content / digests are persisted — a chart-of-accounts
    *digest* (counts + classification breakdown, never balances), the structured
    recommendation drafts already produced by the onboarding planner, and a
    short safe memory summary. No raw financial dumps, raw documents, tokens, or
    secrets are stored. ``_sanitize`` strips obvious secret-bearing keys before
    persistence as a defensive backstop.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional  # noqa: F401

from sqlalchemy.orm import Session

from ..models import (
    CompanyMemory,
    QBOAccount,
    RealmContextArtifact,
    RealmContextProfile,
)
from .onboarding_planner import build_onboarding_plan
from .tenant_companies import organization_company_for_realm, organization_for_company

logger = logging.getLogger("peregrine.realm_context_builder")

PROFILE_VERSION = "v1"

ARTIFACT_KPI = "onboarding_kpi_recommendations"
ARTIFACT_COVENANT = "onboarding_covenant_recommendations"
ARTIFACT_OBJECTIVE = "onboarding_objective_recommendations"
ARTIFACT_COA_DIGEST = "chart_of_accounts_digest"

ONBOARDING_ARTIFACT_TYPES = (
    ARTIFACT_KPI,
    ARTIFACT_COVENANT,
    ARTIFACT_OBJECTIVE,
)

# Keys that must never be persisted in an artifact's content, even if a source
# accidentally surfaces them. Defensive backstop — sources are already summaries.
_SECRET_KEY_HINTS = (
    "token",
    "secret",
    "password",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "authorization",
    "raw_text",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _content_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _sanitize(value: Any) -> Any:
    """Recursively drop secret-bearing keys. Defensive — onboarding drafts are
    already structured summaries, but this guarantees a planted token in any
    source field never lands in a persisted artifact."""
    if isinstance(value, dict):
        clean: Dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(hint in lowered for hint in _SECRET_KEY_HINTS):
                continue
            clean[key] = _sanitize(item)
        return clean
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


class RealmIsolationError(RuntimeError):
    """Raised when a realm cannot be resolved to a full scope triple — building
    realm context without the full triple would risk cross-realm leakage, so we
    refuse rather than scope loosely."""


class RealmContextBuilder:
    """Builds / refreshes the isolated context profile + artifacts for ONE realm.

    Construct with the resolved scope triple (or use ``for_realm`` to resolve it
    from the realm id) and call ``build``."""

    def __init__(
        self,
        db: Session,
        *,
        organization_id: int,
        organization_company_id: int,
        qbo_realm_id: str,
        created_by_agent_run_id: Optional[int] = None,
    ) -> None:
        if organization_id is None or organization_company_id is None or not qbo_realm_id:
            raise RealmIsolationError(
                "RealmContextBuilder requires the full scope triple "
                "(organization_id, organization_company_id, qbo_realm_id)."
            )
        self.db = db
        self.organization_id = int(organization_id)
        self.organization_company_id = int(organization_company_id)
        self.qbo_realm_id = str(qbo_realm_id)
        self.created_by_agent_run_id = created_by_agent_run_id

    # ── resolution ──────────────────────────────────────────────────────────
    @classmethod
    def for_realm(
        cls,
        db: Session,
        qbo_realm_id: str,
        *,
        organization_id: Optional[int] = None,
        organization_company_id: Optional[int] = None,
        created_by_agent_run_id: Optional[int] = None,
    ) -> "RealmContextBuilder":
        """Resolve the full triple from the realm id (and optional hints) and
        return a builder. Raises RealmIsolationError if the realm is not mapped
        to an organization + organization_company."""
        qbo_realm_id = str(qbo_realm_id)
        organization = organization_for_company(db, qbo_realm_id)
        if organization_id is not None and organization and organization.id != organization_id:
            raise RealmIsolationError(
                f"realm {qbo_realm_id} maps to organization {organization.id}, "
                f"not {organization_id}"
            )
        if organization is None:
            raise RealmIsolationError(f"realm {qbo_realm_id} is not mapped to an organization")

        company = organization_company_for_realm(
            db, organization_id=organization.id, realm_id=qbo_realm_id
        )
        if company is None:
            raise RealmIsolationError(
                f"realm {qbo_realm_id} is not mapped to an organization_company"
            )
        if organization_company_id is not None and company.id != organization_company_id:
            raise RealmIsolationError(
                f"realm {qbo_realm_id} maps to organization_company {company.id}, "
                f"not {organization_company_id}"
            )
        return cls(
            db,
            organization_id=organization.id,
            organization_company_id=company.id,
            qbo_realm_id=qbo_realm_id,
            created_by_agent_run_id=created_by_agent_run_id,
        )

    # ── scope helpers ──────────────────────────────────────────────────────
    def _artifact_query(self):
        """Every artifact read is filtered by the full triple — never crosses
        realm boundaries."""
        return self.db.query(RealmContextArtifact).filter(
            RealmContextArtifact.organization_id == self.organization_id,
            RealmContextArtifact.organization_company_id == self.organization_company_id,
            RealmContextArtifact.qbo_realm_id == self.qbo_realm_id,
        )

    def _profile_query(self):
        return self.db.query(RealmContextProfile).filter(
            RealmContextProfile.organization_id == self.organization_id,
            RealmContextProfile.organization_company_id == self.organization_company_id,
            RealmContextProfile.qbo_realm_id == self.qbo_realm_id,
        )

    # ── digests ─────────────────────────────────────────────────────────────
    def _chart_of_accounts_digest(self) -> Dict[str, Any]:
        """Safe COA summary: counts + classification/type breakdown only. Never
        account balances or raw transaction data. Scoped to this realm only."""
        rows = (
            self.db.query(QBOAccount)
            .filter(QBOAccount.realm_id == self.qbo_realm_id, QBOAccount.active == True)  # noqa: E712
            .all()
        )
        by_classification: Dict[str, int] = {}
        by_type: Dict[str, int] = {}
        for row in rows:
            classification = (row.classification or "Unknown").strip() or "Unknown"
            acct_type = (row.account_type or "Unknown").strip() or "Unknown"
            by_classification[classification] = by_classification.get(classification, 0) + 1
            by_type[acct_type] = by_type.get(acct_type, 0) + 1
        return {
            "active_account_count": len(rows),
            "by_classification": by_classification,
            "by_account_type": by_type,
        }

    def _safe_memory_summary(self) -> Dict[str, Any]:
        memory = (
            self.db.query(CompanyMemory)
            .filter(CompanyMemory.company_id == self.qbo_realm_id)
            .filter(
                (CompanyMemory.organization_id == self.organization_id)
                | (CompanyMemory.organization_id.is_(None))
            )
            .first()
        )
        profile_text = (memory.company_profile or "") if memory else ""
        return {
            "has_company_memory": bool(profile_text),
            "profile_preview": profile_text[:280] if profile_text else None,
        }

    # ── persistence ─────────────────────────────────────────────────────────
    def _write_artifact(
        self,
        *,
        artifact_type: str,
        source_type: str,
        content: Any,
        confidence: float,
        source_id: Optional[str] = None,
        source_period: Optional[str] = None,
        freshness_at: Optional[datetime] = None,
    ) -> RealmContextArtifact:
        clean = _sanitize(content)
        artifact = RealmContextArtifact(
            organization_id=self.organization_id,
            organization_company_id=self.organization_company_id,
            qbo_realm_id=self.qbo_realm_id,
            artifact_type=artifact_type,
            source_type=source_type,
            source_id=source_id,
            source_period=source_period,
            freshness_at=freshness_at or _now(),
            content_json=clean,
            content_hash=_content_hash(clean),
            confidence=confidence,
            created_by_agent_run_id=self.created_by_agent_run_id,
        )
        self.db.add(artifact)
        return artifact

    def _replace_onboarding_artifacts(self, plan: Dict[str, Any]) -> None:
        """Replace this realm's onboarding artifacts atomically (delete then
        re-insert), strictly within this realm's scope."""
        self._artifact_query().filter(
            RealmContextArtifact.artifact_type.in_(list(ONBOARDING_ARTIFACT_TYPES))
        ).delete(synchronize_session=False)

        recs = plan.get("recommendations", {})
        freshness = _now()
        self._write_artifact(
            artifact_type=ARTIFACT_KPI,
            source_type="onboarding_planner",
            content={"company_id": self.qbo_realm_id, "custom_kpis": recs.get("custom_kpis", [])},
            confidence=0.7,
            source_id=self.qbo_realm_id,
            freshness_at=freshness,
        )
        self._write_artifact(
            artifact_type=ARTIFACT_COVENANT,
            source_type="onboarding_planner",
            content={"company_id": self.qbo_realm_id, "covenants": recs.get("covenants", [])},
            confidence=0.7,
            source_id=self.qbo_realm_id,
            freshness_at=freshness,
        )
        self._write_artifact(
            artifact_type=ARTIFACT_OBJECTIVE,
            source_type="onboarding_planner",
            content={"company_id": self.qbo_realm_id, "objectives": recs.get("objectives", [])},
            confidence=0.7,
            source_id=self.qbo_realm_id,
            freshness_at=freshness,
        )

    def _upsert_profile(
        self,
        *,
        plan: Dict[str, Any],
        coa_digest: Dict[str, Any],
        memory_summary: Dict[str, Any],
    ) -> RealmContextProfile:
        profile = self._profile_query().first()
        now = _now()
        status_block = plan.get("status", {})
        known_gaps = [
            key
            for key, present in {
                "company_memory": status_block.get("has_company_memory"),
                "chart_of_accounts": bool(status_block.get("account_count")),
                "documents": bool(status_block.get("document_count")),
                "agreements": bool(status_block.get("agreement_count")),
            }.items()
            if not present
        ]
        source_refs = {
            "onboarding_plan": {
                "readiness_score": plan.get("readiness_score"),
                "status": status_block,
            },
            "chart_of_accounts": {"source": "qbo_accounts", "realm_id": self.qbo_realm_id},
        }
        confidence = {
            "onboarding_recommendations": 0.7,
            "chart_of_accounts_digest": 1.0 if coa_digest.get("active_account_count") else 0.0,
        }

        if profile is None:
            profile = RealmContextProfile(
                organization_id=self.organization_id,
                organization_company_id=self.organization_company_id,
                qbo_realm_id=self.qbo_realm_id,
            )
            self.db.add(profile)

        profile.profile_version = PROFILE_VERSION
        profile.status = "ready"
        profile.fiscal_context_json = {"readiness_score": plan.get("readiness_score")}
        profile.chart_of_accounts_digest_json = coa_digest
        profile.module_context_json = {
            "onboarding": {
                "artifact_types": list(ONBOARDING_ARTIFACT_TYPES),
            }
        }
        profile.known_data_gaps_json = {"missing": known_gaps}
        profile.safe_memory_summary_json = memory_summary
        profile.source_refs_json = source_refs
        profile.confidence_json = confidence
        profile.last_refreshed_at = now
        return profile

    # ── entry point ───────────────────────────────────────────────────────
    def build(self) -> Dict[str, Any]:
        """Build / refresh the isolated profile + artifacts for this realm.

        Reuses ``build_onboarding_plan`` (the approved source of KPI/covenant/
        objective drafts) and persists summarized results. Returns a small,
        realm-scoped summary of what was written."""
        plan = build_onboarding_plan(self.db, self.qbo_realm_id)
        coa_digest = self._chart_of_accounts_digest()
        memory_summary = self._safe_memory_summary()

        self._replace_onboarding_artifacts(plan)
        self._write_artifact(
            artifact_type=ARTIFACT_COA_DIGEST,
            source_type="qbo_accounts",
            content=coa_digest,
            confidence=1.0 if coa_digest.get("active_account_count") else 0.0,
            source_id=self.qbo_realm_id,
        )
        # Keep only the latest COA digest for this realm.
        self._artifact_query().filter(
            RealmContextArtifact.artifact_type == ARTIFACT_COA_DIGEST
        ).filter(
            RealmContextArtifact.content_hash != _content_hash(_sanitize(coa_digest))
        ).delete(synchronize_session=False)

        profile = self._upsert_profile(
            plan=plan, coa_digest=coa_digest, memory_summary=memory_summary
        )
        self.db.commit()
        self.db.refresh(profile)

        logger.info(
            "[RealmContextBuilder] built profile id=%s org=%s company=%s realm=%s",
            profile.id,
            self.organization_id,
            self.organization_company_id,
            self.qbo_realm_id,
        )
        return {
            "profile_id": profile.id,
            "organization_id": self.organization_id,
            "organization_company_id": self.organization_company_id,
            "qbo_realm_id": self.qbo_realm_id,
            "status": profile.status,
            "artifact_types": list(ONBOARDING_ARTIFACT_TYPES) + [ARTIFACT_COA_DIGEST],
        }


def build_realm_context(
    db: Session,
    *,
    organization_id: int,
    organization_company_id: int,
    qbo_realm_id: str,
    created_by_agent_run_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Functional entry point used by the Celery task. Requires the full triple."""
    builder = RealmContextBuilder(
        db,
        organization_id=organization_id,
        organization_company_id=organization_company_id,
        qbo_realm_id=qbo_realm_id,
        created_by_agent_run_id=created_by_agent_run_id,
    )
    return builder.build()


# ── fresh-vs-fallback read path used by GET /onboarding/plan ────────────────
def _is_fresh(artifact: RealmContextArtifact, max_age_seconds: int) -> bool:
    if artifact is None:
        return False
    if artifact.expires_at is not None:
        expires = artifact.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < _now():
            return False
    stamp = artifact.freshness_at or artifact.created_at
    if stamp is None:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (_now() - stamp).total_seconds() <= max_age_seconds


def read_precomputed_onboarding_plan(
    db: Session,
    qbo_realm_id: str,
    *,
    max_age_seconds: int = 24 * 60 * 60,
) -> Optional[Dict[str, Any]]:
    """Return a precomputed onboarding plan assembled from fresh, realm-scoped
    artifacts, or ``None`` if no fresh precomputed result exists (cold cache).

    Strictly scoped: resolves the triple and reads only this realm's artifacts.
    Returns ``None`` (caller falls back to live compute) on any ambiguity so the
    response shape is always correct."""
    qbo_realm_id = str(qbo_realm_id)
    organization = organization_for_company(db, qbo_realm_id)
    if organization is None:
        return None
    company = organization_company_for_realm(
        db, organization_id=organization.id, realm_id=qbo_realm_id
    )
    if company is None:
        return None

    def _latest(artifact_type: str) -> Optional[RealmContextArtifact]:
        return (
            db.query(RealmContextArtifact)
            .filter(
                RealmContextArtifact.organization_id == organization.id,
                RealmContextArtifact.organization_company_id == company.id,
                RealmContextArtifact.qbo_realm_id == qbo_realm_id,
                RealmContextArtifact.artifact_type == artifact_type,
            )
            .order_by(RealmContextArtifact.created_at.desc(), RealmContextArtifact.id.desc())
            .first()
        )

    kpi = _latest(ARTIFACT_KPI)
    covenant = _latest(ARTIFACT_COVENANT)
    objective = _latest(ARTIFACT_OBJECTIVE)
    profile = (
        db.query(RealmContextProfile)
        .filter(
            RealmContextProfile.organization_id == organization.id,
            RealmContextProfile.organization_company_id == company.id,
            RealmContextProfile.qbo_realm_id == qbo_realm_id,
        )
        .first()
    )

    if not (kpi and covenant and objective and profile):
        return None
    if not all(_is_fresh(a, max_age_seconds) for a in (kpi, covenant, objective)):
        return None

    status_block = (profile.source_refs_json or {}).get("onboarding_plan", {}).get("status", {})
    readiness = (profile.fiscal_context_json or {}).get("readiness_score")
    memory_summary = profile.safe_memory_summary_json or {}

    return {
        "company_id": qbo_realm_id,
        "readiness_score": readiness,
        "status": {
            "has_company_memory": bool(status_block.get("has_company_memory")),
            "account_count": status_block.get("account_count", 0),
            "document_count": status_block.get("document_count", 0),
            "agreement_count": status_block.get("agreement_count", 0),
            "objective_count": status_block.get("objective_count", 0),
            "custom_kpi_count": status_block.get("custom_kpi_count", 0),
            "active_covenant_count": status_block.get("active_covenant_count", 0),
        },
        "recommendations": {
            "objectives": (objective.content_json or {}).get("objectives", []),
            "custom_kpis": (kpi.content_json or {}).get("custom_kpis", []),
            "covenants": (covenant.content_json or {}).get("covenants", []),
        },
        "briefing_preview": memory_summary.get("profile_preview"),
    }
