import re
from typing import Any, Optional

from fastapi import HTTPException, status

from .auth_schemas import MemberUpdateRequest
from .auth_session import AuthContext
from .models import Organization, OrganizationMembership, User
from .services.auth_migration import stytch_payload_role
from .services.stytch_b2b import StytchB2BError, stytch_b2b_client


def _validate_stytch_authenticated_context(session_payload: dict[str, Any]) -> None:
    member = session_payload.get("member") or {}
    organization = session_payload.get("organization") or {}
    member_status = member.get("status")
    if member_status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Stytch member is not active",
        )
    mfa_policy = organization.get("mfa_policy")
    if (
        mfa_policy == "REQUIRED_FOR_ALL"
        and not member.get("mfa_enrolled")
        and not member.get("is_breakglass")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="MFA enrollment is required for this organization",
        )


def _stytch_mfa_challenge_detail(session_payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    member = session_payload.get("member") or {}
    organization = session_payload.get("organization") or {}
    member_status = member.get("status")
    if member_status is not None and member_status != "active":
        return None

    mfa_required_by_stytch = session_payload.get("member_authenticated") is False
    mfa_required_by_policy = (
        organization.get("mfa_policy") == "REQUIRED_FOR_ALL"
        and not member.get("mfa_enrolled")
        and not member.get("is_breakglass")
    )
    if not (mfa_required_by_stytch or mfa_required_by_policy):
        return None

    organization_id = session_payload.get("organization_id") or organization.get("organization_id")
    member_id = session_payload.get("member_id") or member.get("member_id")
    if not organization_id or not member_id:
        return None

    allowed_methods = organization.get("allowed_mfa_methods") or []
    return {
        "code": "mfa_required",
        "message": "MFA enrollment is required for this organization",
        "organization_id": organization_id,
        "member_id": member_id,
        "intermediate_session_token": session_payload.get("intermediate_session_token"),
        "session_token": session_payload.get("session_token"),
        "primary_required": session_payload.get("primary_required"),
        "mfa_required": session_payload.get("mfa_required"),
        "allowed_mfa_methods": allowed_methods,
    }


def _compact_stytch_payload(payload: dict[str, Any]) -> dict[str, Any]:
    # Drop both None and empty-string values. Stytch rejects empty token fields
    # (e.g. session_token="") with invalid_session_token, and after an
    # MFA-required password reset Stytch returns session_token as "" alongside a
    # real intermediate_session_token. Keep other falsy values (False, 0).
    return {key: value for key, value in payload.items() if value is not None and value != ""}


_E164_PHONE_RE = re.compile(r"^\+[1-9]\d{1,14}$")
_SMS_PHONE_ERROR = "Enter a valid mobile number in E.164 format, like +15555550123."


def _normalize_sms_phone_number(phone_number: Optional[str], *, required: bool = True) -> Optional[str]:
    raw = (phone_number or "").strip()
    if not raw:
        if required:
            raise HTTPException(status_code=400, detail=_SMS_PHONE_ERROR)
        return None

    if raw.startswith("+"):
        candidate = "+" + re.sub(r"\D", "", raw[1:])
    else:
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 10:
            candidate = f"+1{digits}"
        elif len(digits) == 11 and digits.startswith("1"):
            candidate = f"+{digits}"
        else:
            candidate = raw

    if not _E164_PHONE_RE.fullmatch(candidate):
        raise HTTPException(status_code=400, detail=_SMS_PHONE_ERROR)
    return candidate


def _sms_stytch_http_exception(exc: StytchB2BError) -> HTTPException:
    if _stytch_error_type(exc) == "invalid_phone_number":
        return HTTPException(status_code=400, detail=_SMS_PHONE_ERROR)
    return HTTPException(
        status_code=_stytch_error_status(exc),
        detail=_stytch_error_message(exc) or "Could not send SMS verification code",
    )


def _stytch_member_management_error(exc: StytchB2BError) -> str:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    error_type = detail.get("error_type")
    if error_type == "invalid_organization_id":
        return (
            "This tenant is not linked to a valid Stytch B2B organization. "
            "Link the Vesper Bot tenant to the Stytch organization before inviting members."
        )
    message = detail.get("error_message") if isinstance(detail.get("error_message"), str) else None
    lowered_message = (message or "").lower()
    lowered_type = (error_type or "").lower()
    if "already active" in lowered_message or "member_already" in lowered_type:
        return (
            "This email is already an active Stytch member for this tenant. "
            "Remove the member from Organization Members before sending a new invite."
        )
    # A role the app maps to a Stytch role_id that the project's RBAC policy does
    # not define (e.g. canonical 'accounting' -> Stytch 'manager' when 'manager'
    # was never created). Stytch rejects the update; surface an actionable 4xx
    # instead of leaking the raw Stytch error.
    if "role" in lowered_type or "role" in lowered_message:
        return (
            "The requested role could not be assigned in Stytch. Its role_id is "
            "likely missing from this project's RBAC policy. Add the matching "
            "custom role_id (Dashboard -> Roles & Permissions; 'accounting' maps "
            "to Stytch 'manager', 'analyst' to 'analyst', 'viewer' to 'read_only', "
            "'tenant_admin' to 'org_admin'), then retry."
        )
    return message or "Stytch member management request failed"


def _stytch_error_status(exc: StytchB2BError) -> int:
    return exc.status_code if 400 <= exc.status_code < 500 else 502


def _stytch_error_type(exc: StytchB2BError) -> str:
    if isinstance(exc.detail, dict):
        return str(exc.detail.get("error_type") or "")
    return ""


def _stytch_error_message(exc: StytchB2BError) -> str:
    if isinstance(exc.detail, dict):
        return str(exc.detail.get("error_message") or exc.detail)
    return str(exc.detail)


def _is_missing_stytch_member_error(exc: StytchB2BError) -> bool:
    message = _stytch_error_message(exc).lower()
    error_type = _stytch_error_type(exc).lower()
    return exc.status_code == 404 or "not_found" in error_type or "not found" in message


def _has_real_stytch_member_ref(organization: Organization, member_id: str | None) -> bool:
    if not member_id:
        return False
    if member_id == "platform-admin" or member_id.startswith("legacy-member-"):
        return False
    return not organization.stytch_organization_id.startswith("legacy-org-")


def _delete_stytch_member_for_org(organization: Organization, member_id: str) -> bool:
    if not stytch_b2b_client.enabled or not _has_real_stytch_member_ref(organization, member_id):
        return False
    try:
        stytch_b2b_client.delete_member(organization.stytch_organization_id, member_id)
        return True
    except StytchB2BError as exc:
        if _is_missing_stytch_member_error(exc):
            return False
        raise


def _stytch_member_update_payload(payload: MemberUpdateRequest) -> dict[str, Any]:
    body = {k: v for k, v in payload.model_dump(exclude_none=True).items() if k != "reactivate"}
    if "role" in body:
        # Send the Stytch role_id the project defines; the local membership keeps
        # the canonical app role.
        body["roles"] = [stytch_payload_role(body.pop("role"))]
    return body


def _stytch_session_token_for_organization(ctx: AuthContext, organization: Organization) -> Optional[str]:
    if ctx.auth_source != "stytch" or not ctx.token or not ctx.organization:
        return None
    if ctx.organization.stytch_organization_id != organization.stytch_organization_id:
        return None
    return ctx.token


def _can_send_stytch_password_reset(
    *,
    user: Optional[User],
    organization: Optional[Organization],
    membership: Optional[OrganizationMembership],
) -> bool:
    if not stytch_b2b_client.enabled or not organization:
        return False
    if organization.stytch_organization_id.startswith("legacy-org-"):
        return False
    if user is None:
        return True
    if user.auth_source in {"stytch", "hybrid"}:
        return True
    return bool(membership and _has_real_stytch_member_ref(organization, membership.stytch_member_id))
