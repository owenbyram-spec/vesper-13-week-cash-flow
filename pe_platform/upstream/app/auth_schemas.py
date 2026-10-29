from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr


class OrganizationSummary(BaseModel):
    id: int
    stytch_organization_id: str
    slug: str
    name: str
    primary_realm_id: Optional[str] = None
    role: str
    status: str
    mfa_policy: str
    # The organization's MFA method policy; the settings/users form seeds from it, so
    # dropping it here made a save silently replace the stored methods.
    mfa_methods: Optional[str] = None
    allowed_mfa_methods: list[str] = []
    is_active: bool = True
    billing_status: str = "trialing"
    billing_tier: str = "Basic"
    suspended_reason: Optional[str] = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    role: str
    company_ids: list[str] = []
    auth_source: str = "legacy"
    legacy_login_enabled: bool = True
    is_priority_admin: bool = False
    default_organization_id: Optional[int] = None
    force_password_reset: bool = False
    password_reset_required_reason: Optional[str] = None
    active_organization: Optional[OrganizationSummary] = None
    organizations: list[OrganizationSummary] = []
    csrf_token: Optional[str] = None


class AuthUserOut(UserOut):
    token_type: str = "cookie"
    expires_in: int
    csrf_token: Optional[str] = None


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    role: str = "user"
    company_ids: list[str] = []


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    organization_id: Optional[str] = None


class MagicLinkAuthenticateRequest(BaseModel):
    token: str


class UserUpdate(BaseModel):
    role: Optional[str] = None
    password: Optional[str] = None


class ProfileUpdate(BaseModel):
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    # Required with ``password``: a local password change re-authenticates.
    current_password: Optional[str] = None
    default_organization_id: Optional[int] = None


class OrganizationSwitchRequest(BaseModel):
    organization_id: str


class MemberInviteRequest(BaseModel):
    email: EmailStr
    name: Optional[str] = None
    role: str = "viewer"  # canonical least-privilege default
    invite_redirect_url: Optional[str] = None


class MemberUpdateRequest(BaseModel):
    role: Optional[str] = None
    mfa_enrolled: Optional[bool] = None
    is_breakglass: Optional[bool] = None
    reactivate: bool = False


class MemberPasswordResetRequest(BaseModel):
    email: Optional[EmailStr] = None


class TOTPChallengeRequest(BaseModel):
    organization_id: str
    member_id: str
    code: str
    intermediate_session_token: Optional[str] = None
    session_token: Optional[str] = None


class SmsChallengeRequest(BaseModel):
    organization_id: str
    member_id: str
    code: Optional[str] = None
    phone_number: Optional[str] = None
    intermediate_session_token: Optional[str] = None
    session_token: Optional[str] = None


class TOTPEnrollmentStartRequest(BaseModel):
    organization_id: str
    member_id: str
    intermediate_session_token: Optional[str] = None
    session_token: Optional[str] = None


class PlatformOrganizationCreate(BaseModel):
    name: str
    slug: Optional[str] = None
    primary_realm_id: Optional[str] = None
    external_id: Optional[str] = None
    billing_status: str = "trialing"
    billing_tier: str = "Basic"
    billing_customer_id: Optional[str] = None


class PlatformOrganizationUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    primary_realm_id: Optional[str] = None
    external_id: Optional[str] = None
    billing_status: Optional[str] = None
    billing_tier: Optional[str] = None
    billing_customer_id: Optional[str] = None
    is_active: Optional[bool] = None
    suspended_reason: Optional[str] = None


class OrganizationMfaPolicyUpdate(BaseModel):
    mfa_policy: str
    mfa_methods: str = "RESTRICTED"
    allowed_mfa_methods: list[str]


class PasswordResetStartRequest(BaseModel):
    email: EmailStr
    organization_id: Optional[str] = None


class PasswordResetCompleteRequest(BaseModel):
    token: Optional[str] = None
    email: Optional[EmailStr] = None
    current_password: Optional[str] = None
    new_password: str
    organization_id: Optional[str] = None


class EmailChangeRequest(BaseModel):
    new_email: EmailStr


class EmailChangeCompleteRequest(BaseModel):
    token: str
