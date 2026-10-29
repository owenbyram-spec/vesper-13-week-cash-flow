"""
app/services/stripe.py
────────────────────────
Manages Stripe checkout sessions and robust webhook decoding 
for Vesper Bot's B2B monetization flow.
"""
import os
import json
import stripe
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..config import is_dev_environment, settings
from ..models import EntityBilling, Organization, OrganizationMembership, StripeWebhookEvent, TenantBilling
from ..services.auth_migration import record_auth_event
from ..services.redaction import redact_to_string
from ..tenant_rls import pin_tenant_scope_live, tenant_scope_live

logger = logging.getLogger(__name__)

# Configure Stripe globally across the app
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_WEBHOOK_PROCESSING_STALE_AFTER = timedelta(minutes=15)

def is_stripe_configured() -> bool:
    """Check if Stripe has been enabled via ENV vars."""
    return bool(stripe.api_key)


class StripeOperationError(RuntimeError):
    # PLAN-EXEMPT: batch-4 field bug 8 (P0) — live manage-subscription failures
    # masked as a false "not securely configured" 500; root-cause fix, no plan
    # covers app/services. A Stripe API call FAILED — distinct from Stripe being
    # UNCONFIGURED (no api_key). Carries a sanitized, user-safe ``user_message``
    # so the router can surface the REAL reason instead of the misleading
    # "not securely configured" signal. ``customer_missing`` flags the
    # stale/invalid-customer case ("No such customer") so the caller can
    # self-heal by recreating the customer and retrying.
    def __init__(self, user_message: str, *, customer_missing: bool = False):
        super().__init__(user_message)
        self.user_message = user_message
        self.customer_missing = customer_missing


def _stripe_op_error(exc: Exception) -> StripeOperationError:
    """Classify a raw Stripe SDK error into a sanitized StripeOperationError.

    Stripe's ``user_message`` is the safe, customer-facing string; we fall back
    to a generic label so a raw internal trace never reaches the client, and run
    it through ``redact_to_string`` belt-and-suspenders. Customer-missing is
    classified from the RAW exception (param/message) BEFORE redaction."""
    customer_missing = False
    if isinstance(exc, stripe.error.InvalidRequestError):
        param = getattr(exc, "param", "") or ""
        detail = str(getattr(exc, "user_message", "") or exc)
        if param == "customer" or "No such customer" in detail:
            customer_missing = True
    safe = getattr(exc, "user_message", None) or "The billing provider rejected this request."
    return StripeOperationError(redact_to_string(safe), customer_missing=customer_missing)


def _idempotency_key(operation: str, *parts: object) -> str:
    """Build a DETERMINISTIC Stripe Idempotency-Key for a logical operation.

    Stripe stores the first result per key for 24h and replays it on a retry,
    collapsing a double-submit / network-retry into ONE Customer / Session
    instead of minting a duplicate. The key is derived from the STABLE business
    ids of the operation (org / entity / realm / tier / customer) — never a
    fresh uuid4 / attempt id, which would mint a duplicate on every retry. The
    same logical request yields the same key; a different one (a new tier, a
    new entity) yields a distinct key. Stripe keys cap at 255 chars; the joined
    ids stay well under that."""
    joined = ":".join(str(p) for p in parts if p not in (None, ""))
    return f"{operation}:{joined}" if joined else operation


def _minute_bucket() -> str:
    """A coarse UTC minute stamp (YYYYMMDDHHMM) used to scope the idempotency key
    of a session that should be FRESH on each separate open (portal / setup),
    while a true double-submit / SDK network-retry — which lands in the same
    minute — still collapses to one Session. Without it, a deterministic
    customer-only key would replay the FIRST (now stale/expired) session URL on a
    legitimate re-open within Stripe's 24h replay window. Checkout/customer do NOT
    use this (their key is the stable purchase/customer identity — replaying the
    same Session on resume is the desired behavior)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M")


# Fixed-tier monthly prices (cents). Used only when a company has neither a
# Stripe price_id nor a custom amount. An unknown tier must FAIL rather than
# silently bill the highest tier (the old `29900 if tier=="Pro" else 99900`
# overcharged on any typo).
_TIER_PRICE_CENTS: dict[str, int] = {
    "Pro": 29900,
    "Enterprise": 99900,
}


def _default_payment_method_types(enable_ach: bool) -> list[str]:
    """card is always offered; us_bank_account (ACH Direct Debit) is added when
    ACH is enabled for this checkout. ACH must also be enabled on the Stripe
    account for us_bank_account to be accepted."""
    return ["card"] + (["us_bank_account"] if enable_ach else [])


def _resolve_subscription_line_item(
    *,
    tier: str,
    price_id: Optional[str],
    custom_amount_cents: Optional[int],
) -> dict[str, Any]:
    """Build the Checkout line item for a recurring monthly subscription.

    Precedence: explicit Stripe price_id > custom per-company amount > tier table.
    Raises ValueError for an unknown tier or non-positive amount so a bad input
    can never silently charge the wrong price.
    """
    if price_id:
        return {"price": price_id, "quantity": 1}

    if custom_amount_cents is not None:
        amount = int(custom_amount_cents)
    else:
        if tier not in _TIER_PRICE_CENTS:
            raise ValueError(
                f"Unknown billing tier '{tier}'. Provide a Stripe price_id or a "
                f"custom amount, or use one of: {', '.join(sorted(_TIER_PRICE_CENTS))}."
            )
        amount = _TIER_PRICE_CENTS[tier]

    if amount <= 0:
        raise ValueError("Subscription amount (cents) must be a positive integer.")

    return {
        "price_data": {
            "currency": "usd",
            "product_data": {"name": f"Vesper Bot - {tier} Tier"},
            "unit_amount": amount,
            "recurring": {"interval": "month"},
        },
        "quantity": 1,
    }


@dataclass(frozen=True)
class CheckoutTerms:
    """Tier label and price inputs for one Checkout, as passed to create_checkout_session."""

    tier: str
    price_id: Optional[str]
    custom_amount_cents: Optional[int]


def tenant_entity_checkout_terms(billing: EntityBilling, *, requested_tier: str) -> CheckoutTerms:
    """Checkout terms for a TENANT-initiated entity checkout, from server-side state only.

    A tenant picks a tier from the tier table; it never supplies an amount or a
    free-form tier label. A price a platform admin negotiated for the company
    (``stripe_price_id`` / ``custom_monthly_cents``) always applies, labelled with the
    tier the admin stored on the billing row. Request overrides exist only on the
    platform-admin checkout route.
    """
    if requested_tier not in _TIER_PRICE_CENTS:
        raise ValueError(
            f"Unknown billing tier '{requested_tier}'. Choose one of: {', '.join(sorted(_TIER_PRICE_CENTS))}."
        )
    if billing.stripe_price_id or billing.custom_monthly_cents is not None:
        return CheckoutTerms(
            tier=billing.subscription_tier,
            price_id=billing.stripe_price_id,
            custom_amount_cents=billing.custom_monthly_cents,
        )
    return CheckoutTerms(tier=requested_tier, price_id=None, custom_amount_cents=None)


def _utc_from_stripe_timestamp(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None

def create_checkout_session(
    *,
    email: str,
    tier: str,
    success_url: str,
    cancel_url: str,
    organization: Optional[Organization] = None,
    billing: Optional[TenantBilling] = None,
    entity_billing: Optional[EntityBilling] = None,
    price_id: Optional[str] = None,
    custom_amount_cents: Optional[int] = None,
    enable_ach: bool = True,
) -> Optional[str]:
    """
    Spins up a secure Stripe Checkout Session.
    Binds the tenant id in Stripe metadata so webhook events update org billing.

    Pricing precedence: ``price_id`` (Dashboard-managed Stripe Price) >
    ``custom_amount_cents`` (per-company override) > the ``tier`` price table.
    A ValueError from an unknown tier / bad amount is raised to the caller (so
    it can return a 400) rather than swallowed.
    """
    if not is_stripe_configured():
        logger.warning("[Stripe] Cannot create checkout session: STRIPE_SECRET_KEY missing.")
        return None

    # Resolve pricing BEFORE the Stripe call so a bad tier/amount surfaces as a
    # ValueError to the caller instead of a misleading "not configured" None.
    line_item = _resolve_subscription_line_item(
        tier=tier,
        price_id=price_id,
        custom_amount_cents=custom_amount_cents,
    )

    try:
        billing_scope = "entity" if entity_billing else "organization"
        metadata = {
            "tier": tier,
            "billing_scope": billing_scope,
            "organization_id": str(organization.id) if organization else "",
        }
        if entity_billing:
            metadata.update(
                {
                    "organization_company_id": str(entity_billing.organization_company_id),
                    "realm_id": entity_billing.realm_id,
                }
            )
        session_args: dict[str, Any] = {
            "payment_method_types": _default_payment_method_types(enable_ach),
            "line_items": [line_item],
            "mode": "subscription",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "customer_email": email,
            "metadata": metadata,
            "subscription_data": {
                "metadata": metadata,
            },
        }
        customer_id = entity_billing.stripe_customer_id if entity_billing else (billing.stripe_customer_id if billing else None)
        if customer_id:
            session_args.pop("customer_email", None)
            session_args["customer"] = customer_id

        session = stripe.checkout.Session.create(
            **session_args,
            idempotency_key=_idempotency_key(
                "checkout",
                billing_scope,
                metadata.get("organization_id"),
                metadata.get("organization_company_id"),
                metadata.get("realm_id"),
                tier,
            ),
        )
        return session.url
    except stripe.error.StripeError as e:
        # PLAN-EXEMPT: batch-4 bug 8 — surface the REAL failure, never mask it.
        logger.error("[Stripe] Checkout creation failed: %s", redact_to_string(e))
        raise _stripe_op_error(e) from e


def create_stripe_customer(*, email: Optional[str], metadata: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Create a Stripe Customer and return its id (None if Stripe unconfigured)."""
    if not is_stripe_configured():
        return None
    try:
        meta = metadata or {}
        customer = stripe.Customer.create(
            email=email or None,
            metadata=meta,
            idempotency_key=_idempotency_key(
                "customer",
                meta.get("organization_id"),
                meta.get("organization_company_id"),
                meta.get("realm_id"),
                email,
            ),
        )
        return customer.id
    except stripe.error.StripeError as exc:
        # PLAN-EXEMPT: batch-4 bug 8 — surface the REAL failure, never mask it.
        logger.error("[Stripe] Customer creation failed: %s", redact_to_string(exc))
        raise _stripe_op_error(exc) from exc


def create_setup_session(
    *,
    customer_id: str,
    success_url: str,
    cancel_url: str,
    enable_ach: bool = True,
    metadata: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Create a Checkout Session in ``setup`` mode to capture a payment method
    (card and/or ACH bank account) WITHOUT starting a charge. Setup mode
    requires an existing Stripe customer."""
    if not is_stripe_configured():
        logger.warning("[Stripe] Cannot create setup session: STRIPE_SECRET_KEY missing.")
        return None
    if not customer_id:
        return None
    try:
        session = stripe.checkout.Session.create(
            mode="setup",
            payment_method_types=_default_payment_method_types(enable_ach),
            customer=customer_id,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata or {},
            idempotency_key=_idempotency_key("setup", customer_id, _minute_bucket()),
        )
        return session.url
    except stripe.error.StripeError as exc:
        # PLAN-EXEMPT: batch-4 bug 8 — surface the REAL failure, never mask it.
        logger.error("[Stripe] Setup session creation failed: %s", redact_to_string(exc))
        raise _stripe_op_error(exc) from exc


def create_customer_portal_session(
    *,
    customer_id: str,
    return_url: str,
) -> Optional[str]:
    if not is_stripe_configured():
        logger.warning("[Stripe] Cannot create customer portal session: STRIPE_SECRET_KEY missing.")
        return None
    if not customer_id:
        return None
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
            idempotency_key=_idempotency_key("portal", customer_id, _minute_bucket()),
        )
        return session.url
    except stripe.error.StripeError as exc:
        # PLAN-EXEMPT: batch-4 bug 8 — surface the REAL failure, never mask it.
        logger.error("[Stripe] Customer portal session creation failed: %s", redact_to_string(exc))
        raise _stripe_op_error(exc) from exc


def open_session_self_healing(db: Session, billing, *, open_fn) -> str | None:
    # PLAN-EXEMPT: batch-4 bug 8 (P0) — self-heal a stale/invalid Stripe customer
    # id left on a billing row after the live cutover ("No such customer:
    # 'cus_...'"); no ratified plan covers app/services.
    """Open a Stripe hosted session, self-healing a stale/invalid customer id.

    ``open_fn(customer_id)`` builds + creates the session for the billing row's
    current customer id (or None). On a StripeOperationError flagged
    ``customer_missing`` (the "No such customer" case left by a stale TEST id
    after the live cutover), the cached id is nulled on the billing row and the
    open is retried ONCE with ``open_fn(None)`` — the open_fn owns re-provisioning
    (Checkout mints a customer via email; setup/portal create one when None).

    The heal mutation is committed under an explicit tenant pin derived from the
    billing row, so the FORCE-RLS UPDATE on the org-keyed billing table is not
    silently scoped to zero rows. Re-raises StripeOperationError for a real,
    non-self-healable failure so the router surfaces the true reason instead of
    the false 'not configured'."""
    customer_id = getattr(billing, "stripe_customer_id", None) if billing is not None else None
    try:
        return open_fn(customer_id)
    except StripeOperationError as exc:
        if not (exc.customer_missing and billing is not None and customer_id):
            raise
        pin_tenant_scope_live(
            db,
            org_id=getattr(billing, "organization_id", None),
            realm_id=getattr(billing, "realm_id", None),
        )
        billing.stripe_customer_id = None
        billing.stripe_subscription_id = None
        db.commit()
        return open_fn(None)


def cancel_subscription(subscription_id: str) -> bool:
    """Cancel a Stripe subscription immediately. Best-effort: returns True on
    success, False if Stripe is unconfigured or the call fails. Used by the
    per-company delete flow so removing a company's local billing row does not
    orphan an active subscription that keeps charging."""
    if not is_stripe_configured() or not subscription_id:
        return False
    try:
        stripe.Subscription.delete(subscription_id)
        return True
    except Exception as exc:
        logger.error("[Stripe] Subscription cancellation failed sub=%s: %s", subscription_id, exc)
        return False


def _claim_event(db: Session, event: dict[str, Any]) -> tuple[Optional[StripeWebhookEvent], bool]:
    event_id = event.get("id")
    event_type = event.get("type", "unknown")
    if not event_id:
        logger.warning("[Stripe] Webhook event missing id; processing without idempotency ledger")
        return None, False

    existing = db.query(StripeWebhookEvent).filter(StripeWebhookEvent.event_id == event_id).first()
    if existing:
        if existing.status in {"processing", "processed"}:
            if existing.status == "processing" and _stripe_event_is_stale(existing):
                logger.warning("[Stripe] Reclaiming stale processing webhook event_id=%s", event_id)
                existing.status = "processing"
                existing.error = None
                existing.event_type = event_type
                db.commit()
                return existing, False
            logger.info("[Stripe] Duplicate webhook event ignored event_id=%s status=%s", event_id, existing.status)
            return existing, True
        existing.status = "processing"
        existing.error = None
        existing.event_type = event_type
        db.commit()
        return existing, False

    row = StripeWebhookEvent(event_id=event_id, event_type=event_type, status="processing")
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
        return row, False
    except IntegrityError:
        db.rollback()
        logger.info("[Stripe] Duplicate webhook event ignored after concurrent claim event_id=%s", event_id)
        return db.query(StripeWebhookEvent).filter(StripeWebhookEvent.event_id == event_id).first(), True


def _stripe_event_is_stale(event_row: StripeWebhookEvent) -> bool:
    reference = event_row.updated_at or event_row.created_at
    if not reference:
        return False
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - reference > STRIPE_WEBHOOK_PROCESSING_STALE_AFTER


def _mark_event_processed(db: Session, event_row: Optional[StripeWebhookEvent]) -> None:
    if not event_row:
        return
    event_row.status = "processed"
    event_row.error = None
    event_row.processed_at = datetime.now(timezone.utc)
    db.commit()


def _mark_event_failed(db: Session, event_row: Optional[StripeWebhookEvent], exc: Exception) -> None:
    if not event_row:
        return
    db.rollback()
    event_row = db.query(StripeWebhookEvent).filter(StripeWebhookEvent.id == event_row.id).first()
    if event_row:
        event_row.status = "failed"
        event_row.error = redact_to_string(str(exc), max_length=1000)
        db.commit()

def process_webhook_event(payload_bytes: bytes, sig_header: str, db: Session):
    """
    Validates cryptographically that a webhook came from Stripe, then maps
    payment/cancellation events back into our database models.
    """
    if not STRIPE_WEBHOOK_SECRET:
        logger.error("[Stripe] STRIPE_WEBHOOK_SECRET not configured. Cannot process webhook.")
        raise RuntimeError("STRIPE_WEBHOOK_SECRET not configured")

    try:
        verified = stripe.Webhook.construct_event(
            payload_bytes, sig_header, STRIPE_WEBHOOK_SECRET
        )
        if isinstance(verified, dict):
            event = verified
        elif hasattr(verified, "to_dict_recursive"):
            event = verified.to_dict_recursive()
        else:
            event = json.loads(payload_bytes.decode("utf-8"))
    except ValueError as e:
        logger.error("[Stripe] Invalid payload")
        raise e
    except stripe.error.SignatureVerificationError as e:
        logger.error("[Stripe] Invalid signature")
        raise e

    event_row, duplicate = _claim_event(db, event)
    if duplicate:
        return {"status": "duplicate", "event_id": event.get("id")}

    try:
        _apply_webhook_event(event, db)
        _mark_event_processed(db, event_row)
    except Exception as exc:
        _mark_event_failed(db, event_row, exc)
        raise
    return {"status": "processed", "event_id": event.get("id")}


def recover_stale_stripe_webhook_events(db: Session) -> dict[str, Any]:
    """Reclaim StripeWebhookEvent rows stuck in ``processing`` past the stale
    cutoff, re-fetch each event by id from the Stripe API, and re-apply it.

    Parity with the QBO stale-sweep (app/tasks/webhook_recovery.py): without
    this, a Stripe event is only reclaimed opportunistically when a *later*
    webhook for the same entity arrives, so a dead worker can strand a
    billing-state update indefinitely.
    """
    if not is_stripe_configured():
        logger.warning("[Stripe] Stale webhook sweep skipped: STRIPE_SECRET_KEY missing.")
        return {"status": "skipped", "reason": "stripe_not_configured", "recovered": 0, "failed": 0}

    stale_rows = (
        db.query(StripeWebhookEvent)
        .filter(StripeWebhookEvent.status == "processing")
        .order_by(StripeWebhookEvent.id.asc())
        .all()
    )
    recovered = 0
    failed = 0
    for row in stale_rows:
        if not _stripe_event_is_stale(row):
            continue
        try:
            verified = stripe.Event.retrieve(row.event_id)
            if isinstance(verified, dict):
                event = verified
            elif hasattr(verified, "to_dict_recursive"):
                event = verified.to_dict_recursive()
            else:
                event = dict(verified)
            _apply_webhook_event(event, db)
            _mark_event_processed(db, row)
            recovered += 1
        except Exception as exc:  # noqa: BLE001 — one bad event must not stop the sweep
            _mark_event_failed(db, row, exc)
            failed += 1
            logger.error("[Stripe] Stale webhook recovery failed event_id=%s: %s", row.event_id, exc)
    if recovered or failed:
        logger.warning("[Stripe] Stale webhook sweep recovered=%s failed=%s", recovered, failed)
    return {"status": "done", "recovered": recovered, "failed": failed}


def _apply_webhook_event(event: dict[str, Any], db: Session) -> None:
    data = event['data']['object']
    logger.info("[Stripe] Processing webhook event type=%s event_id=%s", event["type"], event.get("id"))

    if event['type'] in _BILLING_MUTATING_EVENTS:
        # PLAN-EXEMPT: NEW P0 field bug (live-prod-corroborated 2026-06-24) — Stripe webhook
        # entity_billing INSERT fails FORCE-RLS WITH CHECK because the pin missed the org.
        org_id, realm_id = _resolve_billing_tenant(db, event)
        if org_id is not None:
            # Pin the resolved org+realm BEFORE the org-keyed TenantBilling/EntityBilling
            # read+write below so they succeed under FORCE RLS. org_id is resolved via the
            # SAME chain _resolve_entity_billing_record uses to set the row's organization_id
            # (metadata.organization_id → organization_company_id → realm_id), so the INSERT's
            # organization_id matches the pinned app.current_org_id and passes WITH CHECK. We
            # also pin realm_id so any realm-keyed sibling write is covered. If the owner is
            # unresolved, fall through to the existing per-branch handling (it logs + no-ops)
            # — never a hard failure, preserving the email-fallback / mismatch-refusal behavior.
            pin_tenant_scope_live(db, org_id=org_id, realm_id=realm_id)

    if event['type'] == 'checkout.session.completed':
        # Payment succeeded! Link the `customer_id` and activate sub
        customer_email = data.get("customer_details", {}).get("email")
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")
        metadata = data.get("metadata") or {}
        organization_id = metadata.get("organization_id")
        organization_company_id = metadata.get("organization_company_id")
        realm_id = metadata.get("realm_id")
        billing_scope = metadata.get("billing_scope") or ("entity" if organization_company_id else "organization")
        # The tier is the one the server derived when it opened this session; with
        # none in the signed metadata the stored label stands rather than a guess.
        tier = metadata.get("tier")

        billing = _resolve_billing_record(
            db,
            billing_scope=billing_scope,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            realm_id=realm_id,
            customer_id=customer_id,
            subscription_id=subscription_id,
            customer_email=customer_email,
        )
        if billing:
            billing.stripe_customer_id = customer_id
            billing.stripe_subscription_id = subscription_id
            billing.subscription_status = "active"
            if tier:
                billing.subscription_tier = tier
            billing.billing_email = customer_email or billing.billing_email
            db.commit()
            logger.info("[Stripe] Successfully activated subscription for organization_id=%s scope=%s", billing.organization_id, billing_scope)
            return
        logger.warning(
            "[Stripe] checkout.session.completed did not resolve billing record organization_id=%s customer_id=%s customer_email=%s",
            organization_id,
            customer_id,
            customer_email,
        )

    elif event['type'] in {'customer.subscription.created', 'customer.subscription.updated'}:
        metadata = data.get("metadata") or {}
        billing = _resolve_billing_record(
            db,
            billing_scope=metadata.get("billing_scope"),
            organization_id=metadata.get("organization_id"),
            organization_company_id=metadata.get("organization_company_id"),
            realm_id=metadata.get("realm_id"),
            customer_id=data.get("customer"),
            subscription_id=data.get("id"),
            customer_email=None,
        )
        if billing:
            billing.stripe_customer_id = data.get("customer") or billing.stripe_customer_id
            billing.stripe_subscription_id = data.get("id") or billing.stripe_subscription_id
            billing.subscription_status = data.get("status") or billing.subscription_status
            billing.subscription_tier = metadata.get("tier") or billing.subscription_tier
            period_start = _utc_from_stripe_timestamp(data.get("current_period_start"))
            period_end = _utc_from_stripe_timestamp(data.get("current_period_end"))
            if period_start:
                billing.current_period_start = period_start
            if period_end:
                billing.current_period_end = period_end
            db.commit()
            return

    elif event['type'] == 'customer.subscription.deleted':
        # Cancellation
        customer_id = data.get("customer")
        metadata = data.get("metadata") or {}
        billing = _resolve_billing_record(
            db,
            billing_scope=metadata.get("billing_scope"),
            organization_id=metadata.get("organization_id"),
            organization_company_id=metadata.get("organization_company_id"),
            realm_id=metadata.get("realm_id"),
            customer_id=customer_id,
            subscription_id=data.get("id"),
            customer_email=None,
        )
        if billing:
            billing.subscription_status = "canceled"
            db.commit()
            logger.info("[Stripe] Successfully canceled subscription for organization_id=%s", billing.organization_id)
            return

    elif event['type'] == 'invoice.payment_failed':
        # Payment failed on automatic recurring cycle
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")
        billing = _resolve_billing_record(
            db,
            billing_scope=None,
            organization_id=None,
            organization_company_id=None,
            realm_id=None,
            customer_id=customer_id,
            subscription_id=subscription_id,
            customer_email=None,
        )
        if billing:
            billing.subscription_status = "past_due"
            db.commit()
            logger.info("[Stripe] Marked organization_id=%s as past_due", billing.organization_id)
            return

    elif event['type'] == 'invoice.paid':
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")
        billing = _resolve_billing_record(
            db,
            billing_scope=None,
            organization_id=None,
            organization_company_id=None,
            realm_id=None,
            customer_id=customer_id,
            subscription_id=subscription_id,
            customer_email=None,
        )
        if billing and billing.subscription_status in {"past_due", "trialing", "incomplete"}:
            billing.subscription_status = "active"
            db.commit()
            logger.info("[Stripe] Reactivated billing record organization_id=%s after invoice.paid", billing.organization_id)
            return

    elif event['type'] == 'customer.subscription.trial_will_end':
        billing = _resolve_billing_record(
            db,
            billing_scope=(data.get("metadata") or {}).get("billing_scope"),
            organization_id=(data.get("metadata") or {}).get("organization_id"),
            organization_company_id=(data.get("metadata") or {}).get("organization_company_id"),
            realm_id=(data.get("metadata") or {}).get("realm_id"),
            customer_id=data.get("customer"),
            subscription_id=data.get("id"),
            customer_email=None,
            create_if_missing=False,  # non-mutating: never create a billing row under FORCE RLS (D7)
        )
        if billing:
            logger.info("[Stripe] Trial ending soon for organization_id=%s subscription_id=%s", billing.organization_id, data.get("id"))


# Only these event types MUTATE org-keyed billing rows and therefore need a pinned scope.
# Non-mutating (trial_will_end) and unhandled event types stay no-ops.
_BILLING_MUTATING_EVENTS = frozenset({
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_failed",
    "invoice.paid",
})


# PLAN-EXEMPT: NEW P0 field bug (live-prod-corroborated 2026-06-24) — pin resolver must
# derive the org from the same chain the INSERT uses, or the entity_billing WITH CHECK fails.
def _resolve_billing_tenant(db: Session, event: dict[str, Any]) -> tuple[int | None, str | None]:  # CRAFT-OK: dynamic Stripe webhook event payload boundary
    """Resolve ``(organization_id, realm_id)`` for a billing-mutating event (READ-ONLY).

    Trusted metadata first — ``organization_id``, then ``organization_company_id``, then
    ``realm_id`` (we set all three at checkout) — otherwise a by-id lookup on
    ``EntityBilling``/``TenantBilling`` by ``stripe_subscription_id`` then ``stripe_customer_id``.
    Resolving the org via ``organization_company_id``/``realm_id`` (NOT only
    ``metadata.organization_id``) is load-bearing: ``_resolve_entity_billing_record`` creates the
    ``EntityBilling`` row with the org it derives from ``organization_company_id``, so the caller's
    PIN must use the SAME chain — otherwise the INSERT's ``organization_id`` fails the org-keyed
    ``WITH CHECK`` under FORCE RLS (the live "new row violates row-level security policy for table
    entity_billing" failure on a first entity subscription whose ``metadata.organization_id`` was
    empty). We also return ``realm_id`` so the caller pins it, covering any realm-keyed sibling
    write. Runs under an EXPLICIT transient ``tenant_scope_live(bypass=True)`` — under FORCE RLS an
    unscoped discovery read fail-closes to zero rows. The bypass is for the ownership LOOKUP only;
    the caller pins the resolved scope before any mutation. Intentionally READ-ONLY: it must NOT
    create a billing row (``_resolve_billing_record``, called later under the pin, does that exactly
    once). The event is already ``construct_event``-verified upstream in ``process_webhook_event``.
    """
    from ..models import OrganizationCompany

    data = event['data']['object']
    etype = event['type']
    metadata = data.get("metadata") or {}
    customer_id = data.get("customer")
    # For subscription.* the object IS the subscription (its id); otherwise the sub id is a
    # nested field. Never use a non-subscription object's own id as a subscription id.
    subscription_id = data.get("id") if etype.startswith("customer.subscription.") else data.get("subscription")
    organization_id = metadata.get("organization_id")
    organization_company_id = metadata.get("organization_company_id")
    realm_id = metadata.get("realm_id") or None

    def _as_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    with tenant_scope_live(db, bypass=True):  # CROSS-TENANT-OK: by-id ownership discovery to resolve the tenant before pinning scope
        oid = _as_int(organization_id)
        if oid and db.query(Organization.id).filter(Organization.id == oid).first():
            return oid, realm_id
        # Entity scope: metadata.organization_id may be absent (e.g. the setup-mode session
        # metadata omits it) — derive the org (and realm) from organization_company_id, the
        # SAME mapping _resolve_entity_billing_record uses to set the inserted row's org.
        ocid = _as_int(organization_company_id)
        if ocid:
            org_company = db.query(OrganizationCompany).filter(OrganizationCompany.id == ocid).first()
            if org_company:
                return org_company.organization_id, (realm_id or (str(org_company.realm_id) if org_company.realm_id else None))
        # Realm-only metadata: resolve the owning org from the realm.
        if realm_id:
            org_company = db.query(OrganizationCompany).filter(OrganizationCompany.realm_id == str(realm_id)).first()
            if org_company:
                return org_company.organization_id, realm_id
        for model in (EntityBilling, TenantBilling):
            if subscription_id:
                row = db.query(model).filter(model.stripe_subscription_id == subscription_id).first()  # SA-OK: bounded-loop 2 — two billing models, not a result-set iteration
                if row:
                    return row.organization_id, (realm_id or getattr(row, "realm_id", None))
            if customer_id:
                row = db.query(model).filter(model.stripe_customer_id == customer_id).first()  # SA-OK: bounded-loop 2 — two billing models, not a result-set iteration
                if row:
                    return row.organization_id, (realm_id or getattr(row, "realm_id", None))
    return None, realm_id


def _resolve_billing_record(
    db: Session,
    *,
    billing_scope: Optional[str] = None,
    organization_id: Optional[str],
    organization_company_id: Optional[str] = None,
    realm_id: Optional[str] = None,
    customer_id: Optional[str],
    subscription_id: Optional[str] = None,
    customer_email: str | None,
    create_if_missing: bool = True,  # False = read-only resolve (find-or-None, never create) — D7
) -> TenantBilling | EntityBilling | None:
    has_entity_metadata = billing_scope == "entity" or organization_company_id or realm_id
    if has_entity_metadata:
        entity_billing = _resolve_entity_billing_record(
            db,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            realm_id=realm_id,
            customer_id=customer_id,
            subscription_id=subscription_id,
            create_if_missing=create_if_missing,
        )
        if entity_billing:
            return entity_billing
        logger.warning(
            "[Stripe] Refusing entity billing webhook with unresolved or mismatched metadata "
            "organization_id=%s organization_company_id=%s realm_id=%s customer_id=%s subscription_id=%s",
            organization_id,
            organization_company_id,
            realm_id,
            customer_id,
            subscription_id,
        )
        return None

    if subscription_id:
        entity_billing = db.query(EntityBilling).filter(EntityBilling.stripe_subscription_id == subscription_id).first()
        if entity_billing:
            return entity_billing
        tenant_billing = db.query(TenantBilling).filter(TenantBilling.stripe_subscription_id == subscription_id).first()
        if tenant_billing:
            return tenant_billing
    if customer_id:
        entity_billing = db.query(EntityBilling).filter(EntityBilling.stripe_customer_id == customer_id).first()
        if entity_billing:
            return entity_billing

    billing = None
    if organization_id:
        try:
            org_id = int(organization_id)
        except (TypeError, ValueError):
            org_id = None
        if org_id:
            billing = db.query(TenantBilling).filter(TenantBilling.organization_id == org_id).first()
            if not billing and create_if_missing:
                org = db.query(Organization).filter(Organization.id == org_id).first()
                if org:
                    billing = TenantBilling(organization_id=org.id)
                    db.add(billing)

    if not billing and customer_id:
        billing = db.query(TenantBilling).filter(TenantBilling.stripe_customer_id == customer_id).first()

    if not billing and customer_email and is_dev_environment(settings.environment):
        membership = (
            db.query(OrganizationMembership)
            .filter(OrganizationMembership.email == customer_email)
            .order_by(OrganizationMembership.is_default.desc(), OrganizationMembership.id.asc())
            .first()
        )
        if membership:
            billing = db.query(TenantBilling).filter(TenantBilling.organization_id == membership.organization_id).first()
            if not billing and create_if_missing:
                billing = TenantBilling(organization_id=membership.organization_id)
                db.add(billing)
            organization = db.query(Organization).filter(Organization.id == membership.organization_id).first()
            logger.warning(
                "[Stripe] Used legacy member-email fallback to resolve tenant billing. "
                "organization_id=%s customer_email=%s",
                membership.organization_id,
                customer_email,
            )
            record_auth_event(
                db,
                event_type="billing_webhook_email_fallback_used",
                organization=organization,
                membership=membership,
                event_status="warning",
                actor_email="stripe:webhook",
                target_email=customer_email,
                details={"reason": "missing_billing_metadata"},
            )
    elif not billing and customer_email:
        logger.warning(
            "[Stripe] Refusing production billing resolution by customer email alone customer_email=%s",
            customer_email,
        )
    return billing


def _resolve_entity_billing_record(
    db: Session,
    *,
    organization_id: Optional[str],
    organization_company_id: Optional[str],
    realm_id: Optional[str],
    customer_id: Optional[str],
    subscription_id: str | None,
    create_if_missing: bool = True,
) -> EntityBilling | None:
    org_id = None
    if organization_id:
        try:
            org_id = int(organization_id)
        except (TypeError, ValueError):
            logger.warning("[Stripe] Invalid organization_id in entity billing metadata: %s", organization_id)
            return None
    org_company_id = None
    if organization_company_id:
        try:
            org_company_id = int(organization_company_id)
        except (TypeError, ValueError):
            logger.warning("[Stripe] Invalid organization_company_id in entity billing metadata: %s", organization_company_id)
            return None

    if subscription_id:
        existing = db.query(EntityBilling).filter(EntityBilling.stripe_subscription_id == subscription_id).first()
        if existing and _entity_billing_matches_metadata(
            existing,
            organization_id=org_id,
            organization_company_id=org_company_id,
            realm_id=realm_id,
        ):
            return existing
        if existing:
            logger.warning("[Stripe] Entity billing subscription metadata mismatch for subscription_id=%s", subscription_id)
            return None
    if customer_id:
        # stripe_customer_id is indexed but NOT unique (unlike stripe_subscription_id),
        # so it is no ceiling. The metadata check below is a runtime guard, not a bound;
        # bind the realm when the webhook supplied one.
        customer_filters = [EntityBilling.stripe_customer_id == customer_id]
        if realm_id:
            customer_filters.append(EntityBilling.realm_id == str(realm_id))
        existing = db.query(EntityBilling).filter(*customer_filters).first()
        if existing and _entity_billing_matches_metadata(
            existing,
            organization_id=org_id,
            organization_company_id=org_company_id,
            realm_id=realm_id,
        ):
            return existing
        if existing:
            logger.warning("[Stripe] Entity billing customer metadata mismatch for customer_id=%s", customer_id)
            return None
    from ..models import OrganizationCompany

    org_company = None
    if org_company_id:
        org_company = db.query(OrganizationCompany).filter(OrganizationCompany.id == org_company_id).first()
        if not org_company:
            return None
        if org_id and org_company.organization_id != org_id:
            logger.warning(
                "[Stripe] Entity billing metadata organization mismatch. organization_id=%s organization_company_id=%s owner_organization_id=%s",
                org_id,
                org_company_id,
                org_company.organization_id,
            )
            return None
        if realm_id and str(org_company.realm_id) != str(realm_id):
            logger.warning(
                "[Stripe] Entity billing metadata realm mismatch. organization_company_id=%s metadata_realm_id=%s owner_realm_id=%s",
                org_company_id,
                realm_id,
                org_company.realm_id,
            )
            return None
        org_id = org_company.organization_id

    if not org_company and org_id and realm_id:
        org_company = (
            db.query(OrganizationCompany)
            .filter(OrganizationCompany.organization_id == org_id, OrganizationCompany.realm_id == str(realm_id))
            .first()
        )
        org_company_id = org_company.id if org_company else None
    if not org_company or not org_id or not org_company_id:
        return None

    billing = db.query(EntityBilling).filter(EntityBilling.organization_company_id == org_company_id).first()
    if not billing and create_if_missing:
        billing = EntityBilling(
            organization_id=org_company.organization_id,
            organization_company_id=org_company_id,
            realm_id=str(org_company.realm_id),
        )
        db.add(billing)
    return billing


def _entity_billing_matches_metadata(
    billing: EntityBilling,
    *,
    organization_id: Optional[int],
    organization_company_id: Optional[int],
    realm_id: Optional[str],
) -> bool:
    if organization_id and billing.organization_id != organization_id:
        return False
    if organization_company_id and billing.organization_company_id != organization_company_id:
        return False
    if realm_id and str(billing.realm_id) != str(realm_id):
        return False
    return True
