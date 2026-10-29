from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    AuthAuditEvent,
    EntityBilling,
    Organization,
    OrganizationCompany,
    OrganizationMembership,
    TenantBilling,
    TenantProvisioningExternalResource,
    TenantProvisioningNotification,
    TenantProvisioningRequest,
    TenantProvisioningRun,
    TenantProvisioningStep,
    User,
)
from ..roles import TENANT_ADMIN, canonical_role
from ..services.auth_migration import build_org_payload, slugify, stytch_payload_role
from ..services.redaction import redact_sensitive, redact_to_string
from ..services.stytch_b2b import stytch_b2b_client


class ProvisioningBlocked(RuntimeError):
    pass


class ProvisioningFailed(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def provisioning_idempotency_key(payload: dict[str, Any]) -> str:
    stable = redact_sensitive(payload)
    raw = json.dumps(stable, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class StytchAdminClient:
    """Thin production seam around Stytch B2B admin calls.

    Tests can monkeypatch this class; when Stytch is not configured we return
    deterministic local identifiers so local/dev provisioning remains dry and
    network-free.
    """

    def create_organization(self, *, name: str, slug: str, primary_realm_id: Optional[str]) -> dict[str, Any]:
        if stytch_b2b_client.enabled:
            payload = build_org_payload(name, slug, primary_realm_id)
            return stytch_b2b_client.create_organization(payload)
        return {
            "organization": {
                "organization_id": f"legacy-org-{slug}",
                "organization_slug": slug,
                "organization_name": name,
                "organization_external_id": primary_realm_id or slug,
                "auth_methods": "RESTRICTED",
                "allowed_auth_methods": [],
                "mfa_policy": settings.stytch_default_mfa_policy,
                "mfa_methods": "RESTRICTED",
                "allowed_mfa_methods": [],
            }
        }

    def invite_initial_admin(
        self,
        *,
        organization_id: str,
        email: str,
        role: str,
    ) -> dict[str, Any]:
        stytch_role = stytch_payload_role(role)
        if stytch_b2b_client.enabled:
            return stytch_b2b_client.invite_member(
                {
                    "organization_id": organization_id,
                    "email_address": email,
                    "roles": [stytch_role],
                }
            )
        return {
            "member": {
                "member_id": f"legacy-member-{hashlib.sha256(f'{organization_id}:{email}'.encode('utf-8')).hexdigest()[:16]}",
                "email_address": email,
                "status": "invited",
                "roles": [stytch_role],
            }
        }


class RenderAdminClient:
    """Dedicated deployment seam.

    The service does not create Render resources unless a future caller passes a
    dedicated, approved payload. The current Phase 4 acceptance path blocks
    dedicated provisioning without explicit platform approval.
    """

    def create_resources(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"status": "not_implemented", "resources": []}


class ProvisioningService:
    def __init__(
        self,
        db: Session,
        *,
        actor: User,
        stytch_client: Optional[StytchAdminClient] = None,
        render_client: Optional[RenderAdminClient] = None,
    ) -> None:
        self.db = db
        self.actor = actor
        self.stytch = stytch_client or StytchAdminClient()
        self.render = render_client or RenderAdminClient()

    def create_request(
        self,
        *,
        payload: dict[str, Any],
        mode: str = "shared",
        idempotency_key: Optional[str] = None,
    ) -> TenantProvisioningRequest:
        key = idempotency_key or provisioning_idempotency_key({"mode": mode, **payload})
        existing = (
            self.db.query(TenantProvisioningRequest)
            .filter(TenantProvisioningRequest.idempotency_key == key)
            .first()
        )
        if existing:
            return existing
        row = TenantProvisioningRequest(
            mode=mode,
            status="pending",
            idempotency_key=key,
            requested_by_user_id=self.actor.id,
            request_payload_json=redact_sensitive(payload),
        )
        self.db.add(row)
        self.db.flush()
        self._audit("tenant_provisioning_requested", request=row, details={"mode": mode})
        self.db.commit()
        self.db.refresh(row)
        return row

    def _completed_step_names(self, request: TenantProvisioningRequest) -> set[str]:
        """Step names that already succeeded in any prior run for this request.

        Used to make retried runs resume at the failed step instead of
        re-executing (and potentially duplicating) earlier side effects.
        """
        rows = (
            self.db.query(TenantProvisioningStep.name)
            .join(TenantProvisioningRun, TenantProvisioningRun.id == TenantProvisioningStep.run_id)
            .filter(
                TenantProvisioningRun.request_id == request.id,
                TenantProvisioningStep.status == "succeeded",
            )
            .all()
        )
        return {name for (name,) in rows}

    def run_request(self, request: TenantProvisioningRequest) -> dict[str, Any]:
        if request.status == "succeeded" and request.organization_id:
            return self._serialize_request(request, reused=True)
        completed_steps = self._completed_step_names(request)
        run = TenantProvisioningRun(request_id=request.id, status="running", started_at=utcnow())
        self.db.add(run)
        self.db.flush()
        request.status = "running"
        self.db.flush()
        self.db.commit()
        self.db.refresh(request)
        self.db.refresh(run)
        current_step = "validate_inputs"
        try:
            payload = dict(request.request_payload_json or {})
            if request.mode == "dedicated_render" and not bool(payload.get("platform_approved")):
                raise ProvisioningBlocked("Dedicated Render provisioning requires explicit platform approval")

            self._step(run, "validate_inputs", {"mode": request.mode, "slug": payload.get("slug")})
            self.db.commit()

            if request.mode == "dedicated_render":
                # Dedicated Render provisioning is a stub: fail the request
                # cleanly with an explicit not_implemented step failure before
                # any shared resources are created, instead of blocking.
                current_step = "render_resources"
                render_result = self.render.create_resources(payload)
                if (render_result or {}).get("status") == "not_implemented":
                    raise ProvisioningFailed(
                        "render_resources not_implemented: dedicated Render provisioning is not implemented yet"
                    )
                self._step(run, "render_resources", render_result)
                self.db.commit()

            current_step = "organization"
            org = self._provision_organization(request, run, payload, completed_steps)
            self.db.commit()
            current_step = "organization_company"
            org_company = self._provision_company(request, run, org, payload, completed_steps)
            self.db.commit()
            current_step = "billing"
            billing = self._provision_billing(run, org, org_company, payload, completed_steps)
            self.db.commit()
            current_step = "initial_admin"
            user, membership = self._provision_initial_admin(run, org, payload, completed_steps)
            self.db.commit()
            current_step = "notification"
            self._provision_notifications(request, run, payload, membership, completed_steps)
            self.db.commit()
            current_step = "finalize"

            org.is_active = True
            request.organization_id = org.id
            request.organization_company_id = org_company.id if org_company else None
            request.status = "succeeded"
            request.error = None
            run.status = "succeeded"
            run.finished_at = utcnow()
            self._audit(
                "tenant_provisioning_succeeded",
                request=request,
                organization=org,
                details={
                    "organization_id": org.id,
                    "organization_company_id": org_company.id if org_company else None,
                    "initial_admin_user_id": user.id,
                    "membership_id": membership.id,
                    "billing_id": billing.id if billing else None,
                },
            )
            self.db.commit()
            self.db.refresh(request)
            return self._serialize_request(request)
        except ProvisioningBlocked as exc:
            self.db.rollback()
            request = self.db.query(TenantProvisioningRequest).filter(TenantProvisioningRequest.id == request.id).one()
            run = self.db.query(TenantProvisioningRun).filter(TenantProvisioningRun.id == run.id).one()
            safe_error = redact_to_string(str(exc), max_length=1000)
            request.status = "blocked"
            request.error = safe_error
            run.status = "blocked"
            run.error = safe_error
            run.finished_at = utcnow()
            self._audit("tenant_provisioning_blocked", request=request, details={"reason": safe_error})
            self.db.commit()
            raise
        except Exception as exc:
            self.db.rollback()
            request = self.db.query(TenantProvisioningRequest).filter(TenantProvisioningRequest.id == request.id).one()
            run = self.db.query(TenantProvisioningRun).filter(TenantProvisioningRun.id == run.id).one()
            safe_error = redact_to_string(str(exc), max_length=1000)
            self._record_failed_step(run, current_step, safe_error)
            request.status = "failed"
            request.error = safe_error
            run.status = "failed"
            run.error = safe_error
            run.finished_at = utcnow()
            self._audit(
                "tenant_provisioning_failed",
                request=request,
                details={"reason": safe_error, "failed_step": current_step},
            )
            self.db.commit()
            raise ProvisioningFailed(safe_error) from exc

    def _provision_organization(
        self,
        request: TenantProvisioningRequest,
        run: TenantProvisioningRun,
        payload: dict[str, Any],
        completed_steps: Optional[set[str]] = None,
    ) -> Organization:
        name = str(payload.get("organization_name") or "").strip()
        if not name:
            raise ProvisioningFailed("organization_name is required")
        slug = slugify(str(payload.get("slug") or name))
        primary_realm_id = payload.get("initial_realm_id")
        existing = self.db.query(Organization).filter(Organization.slug == slug).first()
        if existing:
            status = "resumed" if completed_steps and "organization" in completed_steps else "reused"
            self._step(run, "organization", {"status": status, "organization_id": existing.id})
            return existing

        stytch_resp = self.stytch.create_organization(name=name, slug=slug, primary_realm_id=primary_realm_id)
        org_data = stytch_resp.get("organization") or {}
        org = Organization(
            stytch_organization_id=org_data.get("organization_id") or f"legacy-org-{slug}",
            slug=org_data.get("organization_slug") or slug,
            name=org_data.get("organization_name") or name,
            external_id=org_data.get("organization_external_id") or primary_realm_id or slug,
            primary_realm_id=primary_realm_id,
            auth_methods=org_data.get("auth_methods") or "RESTRICTED",
            allowed_auth_methods=org_data.get("allowed_auth_methods") or [],
            mfa_policy=org_data.get("mfa_policy") or settings.stytch_default_mfa_policy,
            mfa_methods=org_data.get("mfa_methods") or "RESTRICTED",
            allowed_mfa_methods=org_data.get("allowed_mfa_methods") or [],
            # is_active/billing_status are the BILLING axis; status is the independent
            # clickwrap lifecycle axis (plan 20260720-clickwrap-backend-spine, DEC-F5).
            # A provisioned org starts pending_acceptance and only reaches "active" via a
            # recorded Tier-1 assent — do NOT conflate the two axes.
            is_active=False,
            billing_status=payload.get("billing_status") or "trialing",
            billing_tier=payload.get("billing_tier") or "Basic",
            status="pending_acceptance",
        )
        self.db.add(org)
        self.db.flush()
        self._resource(request, run, "stytch", "organization", org.stytch_organization_id, {"slug": org.slug})
        self._step(run, "organization", {"status": "created", "organization_id": org.id})
        return org

    def _provision_company(
        self,
        request: TenantProvisioningRequest,
        run: TenantProvisioningRun,
        org: Organization,
        payload: dict[str, Any],
        completed_steps: Optional[set[str]] = None,
    ) -> Optional[OrganizationCompany]:
        realm_id = payload.get("initial_realm_id")
        if not realm_id:
            self._step(run, "organization_company", {"status": "skipped", "reason": "no_initial_realm"})
            return None
        company = (
            self.db.query(OrganizationCompany)
            .filter(OrganizationCompany.realm_id == str(realm_id))
            .first()
        )
        if company:
            if company.organization_id != org.id:
                raise ProvisioningFailed("initial_realm_id is already assigned to another organization")
            status = "resumed" if completed_steps and "organization_company" in completed_steps else "reused"
            self._step(run, "organization_company", {"status": status, "organization_company_id": company.id})
            return company
        company = OrganizationCompany(
            organization_id=org.id,
            realm_id=str(realm_id),
            display_name=payload.get("initial_company_name") or org.name,
            is_primary=True,
            status="active",
        )
        self.db.add(company)
        self.db.flush()
        self._step(run, "organization_company", {"status": "created", "organization_company_id": company.id})
        return company

    def _provision_billing(
        self,
        run: TenantProvisioningRun,
        org: Organization,
        company: Optional[OrganizationCompany],
        payload: dict[str, Any],
        completed_steps: Optional[set[str]] = None,
    ) -> TenantBilling:
        billing = self.db.query(TenantBilling).filter(TenantBilling.organization_id == org.id).first()
        if not billing:
            billing = TenantBilling(
                organization_id=org.id,
                subscription_status=payload.get("billing_status") or "trialing",
                subscription_tier=payload.get("billing_tier") or "Basic",
                billing_email=payload.get("admin_email"),
            )
            self.db.add(billing)
            self.db.flush()
        if company:
            entity_billing = (
                self.db.query(EntityBilling)
                .filter(EntityBilling.organization_company_id == company.id)
                .first()
            )
            if not entity_billing:
                self.db.add(
                    EntityBilling(
                        organization_id=org.id,
                        organization_company_id=company.id,
                        realm_id=company.realm_id,
                        subscription_status=payload.get("billing_status") or "trialing",
                        subscription_tier=payload.get("billing_tier") or "Basic",
                        billing_email=payload.get("admin_email"),
                    )
                )
        self._step(run, "billing", {"status": "ready", "organization_id": org.id, "company_id": company.id if company else None})
        return billing

    def _provision_initial_admin(
        self,
        run: TenantProvisioningRun,
        org: Organization,
        payload: dict[str, Any],
        completed_steps: Optional[set[str]] = None,
    ) -> tuple[User, OrganizationMembership]:
        email = str(payload.get("admin_email") or "").strip().lower()
        if not email:
            raise ProvisioningFailed("admin_email is required")
        role = canonical_role(payload.get("admin_role") or TENANT_ADMIN)
        user = self.db.query(User).filter(User.email == email).first()
        if not user:
            user = User(
                email=email,
                password_hash="stytch-managed",
                role="user",
                display_name=payload.get("admin_name"),
                auth_source="stytch",
                legacy_login_enabled=False,
                default_organization_id=org.id,
            )
            self.db.add(user)
            self.db.flush()
        elif user.default_organization_id is None:
            user.default_organization_id = org.id

        existing_membership = (
            self.db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.organization_id == org.id,
                OrganizationMembership.user_id == user.id,
            )
            .first()
        )
        if completed_steps and "initial_admin" in completed_steps and existing_membership:
            # Step already succeeded in a prior run — never re-invite via Stytch.
            self._step(
                run,
                "initial_admin",
                {"status": "resumed", "user_id": user.id, "membership_id": existing_membership.id},
            )
            return user, existing_membership

        invite_resp = self.stytch.invite_initial_admin(
            organization_id=org.stytch_organization_id,
            email=email,
            role=role,
        )
        member = invite_resp.get("member") or {}
        membership = existing_membership
        if not membership:
            membership = OrganizationMembership(
                organization_id=org.id,
                user_id=user.id,
                stytch_member_id=member.get("member_id") or f"legacy-member-{user.id}-{org.id}",
                email=email,
                role=role,
                status=member.get("status") or "invited",
            )
            self.db.add(membership)
        else:
            membership.role = role
            membership.status = membership.status or member.get("status") or "invited"
            if member.get("member_id"):
                membership.stytch_member_id = member["member_id"]
        self.db.flush()
        self._step(run, "initial_admin", {"status": "invited", "user_id": user.id, "membership_id": membership.id})
        return user, membership

    def _provision_notifications(
        self,
        request: TenantProvisioningRequest,
        run: TenantProvisioningRun,
        payload: dict[str, Any],
        membership: OrganizationMembership,
        completed_steps: Optional[set[str]] = None,
    ) -> None:
        exists = (
            self.db.query(TenantProvisioningNotification)
            .filter(
                TenantProvisioningNotification.request_id == request.id,
                TenantProvisioningNotification.recipient == membership.email,
                TenantProvisioningNotification.channel == "stytch_invite",
            )
            .first()
        )
        if not exists:
            self.db.add(
                TenantProvisioningNotification(
                    request_id=request.id,
                    run_id=run.id,
                    channel="stytch_invite",
                    recipient=membership.email,
                    status="sent",
                    payload_json={"organization_id": membership.organization_id, "role": membership.role},
                )
            )
        self._step(run, "notification", {"status": "sent", "recipient": membership.email})

    def _step(self, run: TenantProvisioningRun, name: str, details: dict[str, Any]) -> TenantProvisioningStep:
        step = TenantProvisioningStep(
            run_id=run.id,
            name=name,
            status="succeeded",
            details_json=redact_sensitive(details),
            started_at=utcnow(),
            finished_at=utcnow(),
        )
        self.db.add(step)
        self.db.flush()
        return step

    def _record_failed_step(self, run: TenantProvisioningRun, name: str, error: str) -> TenantProvisioningStep:
        step = TenantProvisioningStep(
            run_id=run.id,
            name=name,
            status="failed",
            details_json={},
            error=error,
            started_at=utcnow(),
            finished_at=utcnow(),
        )
        self.db.add(step)
        self.db.flush()
        return step

    def _resource(
        self,
        request: TenantProvisioningRequest,
        run: TenantProvisioningRun,
        provider: str,
        resource_type: str,
        resource_id: str,
        metadata: dict[str, Any],
    ) -> TenantProvisioningExternalResource:
        existing = (
            self.db.query(TenantProvisioningExternalResource)
            .filter(
                TenantProvisioningExternalResource.provider == provider,
                TenantProvisioningExternalResource.resource_type == resource_type,
                TenantProvisioningExternalResource.resource_id == resource_id,
            )
            .first()
        )
        if existing:
            return existing
        row = TenantProvisioningExternalResource(
            request_id=request.id,
            run_id=run.id,
            provider=provider,
            resource_type=resource_type,
            resource_id=resource_id,
            status="ready",
            metadata_json=redact_sensitive(metadata),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _audit(
        self,
        event_type: str,
        *,
        request: TenantProvisioningRequest,
        organization: Optional[Organization] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        self.db.add(
            AuthAuditEvent(
                user_id=self.actor.id,
                organization_id=organization.id if organization else request.organization_id,
                event_type=event_type,
                event_status="success",
                actor_email=self.actor.email,
                details=redact_sensitive(
                    {
                        "request_id": request.id,
                        "mode": request.mode,
                        **(details or {}),
                    }
                ),
            )
        )

    def _serialize_request(self, request: TenantProvisioningRequest, *, reused: bool = False) -> dict[str, Any]:
        runs = (
            self.db.query(TenantProvisioningRun)
            .filter(TenantProvisioningRun.request_id == request.id)
            .order_by(TenantProvisioningRun.id.asc())
            .all()
        )
        return {
            "id": request.id,
            "mode": request.mode,
            "status": request.status,
            "idempotency_key": request.idempotency_key,
            "organization_id": request.organization_id,
            "organization_company_id": request.organization_company_id,
            "error": request.error,
            "reused": reused,
            "runs": [
                {
                    "id": run.id,
                    "status": run.status,
                    "error": run.error,
                    "steps": [
                        {
                            "name": step.name,
                            "status": step.status,
                            "details": step.details_json,
                            "error": step.error,
                        }
                        for step in self.db.query(TenantProvisioningStep)
                        .filter(TenantProvisioningStep.run_id == run.id)
                        .order_by(TenantProvisioningStep.id.asc())
                        .all()
                    ],
                }
                for run in runs
            ],
        }
