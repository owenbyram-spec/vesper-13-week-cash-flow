"""
app/services/email_service.py
─────────────────────────────
Handles outbound email notifications.

  - Approval emails: HTML email with single-use approve/reject links
  - Weekly digest: summarizes KPIs, open proposals, anomalies
  - Slack webhook: optional Slack notification

Configuration lives in SystemSettings per company (email_smtp_host, etc.)
or falls back to environment variables (EMAIL_* prefix).
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import secrets
import smtplib
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Any, Dict, List, Optional

import httpx

from .crypto import decrypt_secret
from .redaction import redact_to_string
from .task_autonomy import latest_locked_period
from sqlalchemy.orm import Session

from ..models import ApprovalToken, SystemSettings, InsightKPI, InsightAnomaly, ProposedAction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DebugNotificationResult:
    delivered: bool
    reason: Optional[str] = None

# ─────────────────────────────────────────────────────────────────────────────
# Token helpers (single-use bearer tokens; only hashes are stored)
# ─────────────────────────────────────────────────────────────────────────────

def _hash_approval_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def make_approval_token(
    db: Session,
    company_id: str,
    action_id: int,
    decision: str,
    recipient_email: Optional[str],
    ttl_seconds: int = 86400 * 7,
) -> str:
    """Create a single-use token for one-click approve/reject."""
    action = db.query(ProposedAction).filter(ProposedAction.id == action_id, ProposedAction.company_id == company_id).first()
    if action is None:
        raise ValueError("proposal not found in token company")
    secret = secrets.token_urlsafe(32)
    token_row = ApprovalToken(
        token_hash=_hash_approval_secret(secret),
        action_id=action_id,
        proposal_revision=action.revision,
        company_id=company_id,
        decision=decision,
        recipient_email=recipient_email.lower() if recipient_email else None,
        expires_at=datetime.utcnow() + timedelta(seconds=ttl_seconds),
    )
    db.add(token_row)
    db.flush()
    raw = json.dumps({"id": token_row.id, "secret": secret}, separators=(",", ":"))
    return urllib.parse.quote(raw, safe="")


def verify_approval_token(
    db: Session,
    token: str,
    expected_decision: Optional[str] = None,
) -> Optional[ApprovalToken]:
    """Verify a single-use approval token. The caller marks it used after applying the decision."""
    try:
        raw = urllib.parse.unquote(token)
        data = json.loads(raw)
        token_id = data["id"]
        secret = data["secret"]
        token_hash = _hash_approval_secret(secret)

        token_row = db.query(ApprovalToken).filter(ApprovalToken.id == token_id).first()
        if not token_row:
            logger.warning("Approval token not found")
            return None
        if token_row.used_at:
            logger.warning("Approval token already used")
            return None
        if token_row.expires_at.replace(tzinfo=None) < datetime.utcnow():
            logger.warning("Approval token expired")
            return None
        if expected_decision and token_row.decision != expected_decision:
            logger.warning("Approval token decision mismatch")
            return None
        if not secrets.compare_digest(token_row.token_hash, token_hash):
            logger.warning("Approval token signature mismatch")
            return None

        return token_row
    except Exception as e:
        logger.exception(f"Token parse error: {e}")
        return None


def is_used_approval_token(
    db: Session,
    token: str,
    expected_decision: Optional[str] = None,
) -> bool:
    """Return True iff ``token`` is a genuine, already-used approval token.

    Used by the POST confirm routes to render a friendly "already processed" page
    when a recipient re-clicks a one-click link, instead of a generic error.

    The secret must still match (constant-time) so this never leaks the existence
    of a token to a caller who does not hold the original secret — an attacker
    guessing token ids gets the same generic error as for any invalid token.
    """
    try:
        raw = urllib.parse.unquote(token)
        data = json.loads(raw)
        token_row = db.query(ApprovalToken).filter(ApprovalToken.id == data["id"]).first()
        if not token_row or not token_row.used_at:
            return False
        if expected_decision and token_row.decision != expected_decision:
            return False
        token_hash = _hash_approval_secret(data["secret"])
        return secrets.compare_digest(token_row.token_hash, token_hash)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# SMTP helper
# ─────────────────────────────────────────────────────────────────────────────

def _get_smtp_config(db: Session, company_id: str) -> Optional[Dict[str, Any]]:
    """Get SMTP config from SystemSettings, falling back to env vars."""
    settings = db.query(SystemSettings).filter(
        SystemSettings.realm_id == company_id
    ).first()

    host = (settings.email_smtp_host if settings else None) or os.getenv("EMAIL_SMTP_HOST")
    port = (settings.email_smtp_port if settings else None) or int(os.getenv("EMAIL_SMTP_PORT", "587"))
    user = (settings.email_smtp_user if settings else None) or os.getenv("EMAIL_SMTP_USER")
    # Decrypt at read (idempotent: plaintext passes through, enc:v1 ciphertext is
    # decrypted) — the stored value is encrypted by the AC-C3 backfill migration.
    password = decrypt_secret(settings.email_smtp_pass if settings else None) or os.getenv("EMAIL_SMTP_PASS")
    recipient = (settings.email_approver if settings else None) or os.getenv("EMAIL_APPROVER")

    if not host or not recipient:
        return None

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "recipient": recipient,
        "slack_webhook": settings.slack_webhook_url if settings else os.getenv("SLACK_WEBHOOK_URL"),
    }


@dataclass(frozen=True)
class SMTPDeliveryResult:
    status: str
    reason: str | None = None


def _send_email_detailed(smtp: Dict[str, Any], subject: str, html_body: str, *, message_id: str | None = None) -> SMTPDeliveryResult:
    """Track the acceptance boundary. QUIT failure never revokes a DATA receipt."""
    server = None
    entered_send = False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp["user"] or "vesper-bot@example.invalid"
        recipients = [part.strip() for part in smtp["recipient"].split(",") if part.strip()]
        msg["To"] = ", ".join(recipients)
        if message_id:
            msg["Message-ID"] = message_id
        msg.attach(MIMEText(html_body, "html"))
        server = smtplib.SMTP(smtp["host"], smtp["port"], timeout=30)
        server.ehlo()
        server.starttls()
        if smtp["user"] and smtp["password"]:
            server.login(smtp["user"], smtp["password"])
        entered_send = True
        refused = server.sendmail(msg["From"], recipients, msg.as_string())
        return SMTPDeliveryResult("partial" if refused else "sent", "some_recipients_refused" if refused else None)
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused):
        return SMTPDeliveryResult("retryable", "smtp_pre_data_refusal")
    except smtplib.SMTPDataError:
        return SMTPDeliveryResult("failed", "smtp_data_rejected")
    except Exception:
        return SMTPDeliveryResult("uncertain" if entered_send else "retryable", "smtp_acceptance_unknown" if entered_send else "smtp_pre_data_failure")
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                # Closing a connection is best effort; accepted DATA is final.
                try:
                    server.close()
                except Exception:
                    pass


def _send_email(smtp: Dict[str, Any], subject: str, html_body: str) -> bool:
    return _send_email_detailed(smtp, subject, html_body).status in ("sent", "partial")


def _send_slack(webhook_url: str, message: str) -> bool:
    try:
        r = httpx.post(webhook_url, json={"text": message}, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.exception(f"Slack webhook failed: {e}")
        return False


def _debug_notification_subject(payload: dict[str, Any]) -> str:
    level = str(payload.get("level") or "error").upper()
    project = str(payload.get("project_slug") or "unknown")
    environment = str(payload.get("environment") or "unknown")
    title = str(payload.get("title") or "Debug incident")
    return f"Vesper Bot debug alert: {level} {project}/{environment} - {title[:80]}"


def _debug_notification_html(payload: dict[str, Any]) -> str:
    fields = {
        "Level": payload.get("level"),
        "Project": payload.get("project_slug"),
        "Environment": payload.get("environment"),
        "Sentry event": payload.get("sentry_event_id"),
        "Incident ID": payload.get("incident_id"),
    }
    rows = "".join(
        f"<tr><th align=\"left\">{html.escape(label)}</th><td>{html.escape(str(value or ''))}</td></tr>"
        for label, value in fields.items()
    )
    return f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 640px; margin: 0 auto; padding: 20px;">
      <h2>Vesper Bot debug alert</h2>
      <p>{html.escape(str(payload.get("title") or "Debug incident"))}</p>
      <table cellpadding="6" cellspacing="0" style="border-collapse: collapse;">{rows}</table>
      <p><strong>Triage:</strong> {html.escape(str(payload.get("triage_summary") or ""))}</p>
    </body></html>
    """


def _debug_notification_slack_text(payload: dict[str, Any]) -> str:
    return (
        f"*Vesper Bot debug alert* "
        f"{str(payload.get('level') or 'error').upper()} "
        f"{payload.get('project_slug') or 'unknown'}/{payload.get('environment') or 'unknown'}\n"
        f">{payload.get('title') or 'Debug incident'}\n"
        f"{payload.get('triage_summary') or ''}"
    )


def send_debug_incident_notification(*, recipient: str, channel: str, payload: dict[str, Any]) -> DebugNotificationResult:
    """Send one sanitized debug incident notification over the configured transport."""
    normalized_channel = (channel or "email").strip().lower()
    if normalized_channel == "email":
        host = os.getenv("EMAIL_SMTP_HOST")
        if not host:
            return DebugNotificationResult(False, "email_smtp_unconfigured")
        try:
            port = int(os.getenv("EMAIL_SMTP_PORT", "587"))
        except ValueError:
            return DebugNotificationResult(False, "email_smtp_port_invalid")
        smtp = {
            "host": host,
            "port": port,
            "user": os.getenv("EMAIL_SMTP_USER"),
            "password": os.getenv("EMAIL_SMTP_PASS"),
            "recipient": recipient,
        }
        sent = _send_email(smtp, _debug_notification_subject(payload), _debug_notification_html(payload))
        return DebugNotificationResult(sent, None if sent else "email_send_failed")

    if normalized_channel == "slack":
        webhook_url = os.getenv("DEBUG_NOTIFICATION_SLACK_WEBHOOK_URL") or os.getenv("SLACK_WEBHOOK_URL")
        if not webhook_url:
            return DebugNotificationResult(False, "slack_webhook_unconfigured")
        sent = _send_slack(webhook_url, _debug_notification_slack_text(payload))
        return DebugNotificationResult(sent, None if sent else "slack_send_failed")

    return DebugNotificationResult(False, "unsupported_channel")


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def send_approval_email(
    db: Session,
    company_id: str,
    action_id: int,
    action_summary: str,
    reasoning: str,
    base_url: Optional[str] = None,
) -> bool:
    """
    Send an HTML email with one-click approve/reject links for a ProposedAction.

    ── SECURITY: DO NOT WIRE A CALLER TO THIS WITHOUT A RATIFIED REDESIGN ──────
    This function is deliberately UNCALLED in production (verified 2026-07-10,
    finding F-06): the links it mints are FULL AUTHORIZATION to approve/reject a
    financial write-back with no session, no MFA, and a 7-day TTL — anyone who
    can read the recipient's inbox (or the Slack channel) can approve the action.
    ``make_approval_token`` is only ever minted here, so leaving this unwired is
    exactly what keeps the /actions/approve-token/... lane unreachable. Before
    adding ANY caller: require a short-TTL (~minutes) decision-bound token, an
    authenticated confirmation step (session or step-up MFA) on the POST, and a
    ratified plan covering the flow. See the F-06 research note in
    work/demo-prep/2026-07-10-security-remediation-results.md.
    """
    smtp = _get_smtp_config(db, company_id)
    if not smtp:
        logger.info(f"No email config for company {company_id}; skipping approval email")
        return False

    base = base_url or os.getenv("APP_BASE_URL", "http://localhost:8000")
    recipient = smtp.get("recipient")
    approve_token = make_approval_token(db, company_id, action_id, "APPROVED", recipient)
    reject_token = make_approval_token(db, company_id, action_id, "REJECTED", recipient)
    db.commit()
    # These links land on a GET confirmation page (no state change on click);
    # the recipient then confirms via a POST form to approve/reject.
    approve_url = f"{base}/api/actions/approve-token/{approve_token}"
    reject_url = f"{base}/api/actions/reject-token/{reject_token}"

    html = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
      <div style="background: #1e3a5f; padding: 24px; border-radius: 8px 8px 0 0;">
        <h1 style="color: white; margin: 0; font-size: 20px;">🦅 Vesper Bot — Action Requires Approval</h1>
      </div>
      <div style="border: 1px solid #e0e0e0; border-top: none; padding: 24px; border-radius: 0 0 8px 8px;">
        <p style="color: #333; font-size: 15px;">{action_summary}</p>
        <div style="background: #f8f9fa; border-left: 4px solid #1e3a5f; padding: 12px; margin: 16px 0;">
          <p style="margin: 0; font-size: 13px; color: #555;"><strong>AI Reasoning:</strong> {reasoning}</p>
        </div>
        <div style="margin-top: 24px; display: flex; gap: 12px;">
          <a href="{approve_url}" style="background: #27ae60; color: white; padding: 12px 28px; border-radius: 6px; text-decoration: none; font-weight: bold; margin-right: 12px;">
            ✅ Approve
          </a>
          <a href="{reject_url}" style="background: #e74c3c; color: white; padding: 12px 28px; border-radius: 6px; text-decoration: none; font-weight: bold;">
            ❌ Reject
          </a>
        </div>
        <p style="margin-top: 20px; font-size: 12px; color: #999;">
          This link expires in 7 days. You can also review this proposal in the
          <a href="{base}/actions" style="color: #1e3a5f;">Vesper Bot dashboard</a>.
        </p>
      </div>
    </body></html>
    """

    subject = f"Vesper Bot: Action Requires Approval — {action_summary[:60]}"
    sent = _send_email(smtp, subject, html)

    # Also send Slack notification
    if smtp.get("slack_webhook"):
        slack_msg = (
            f"🦅 *Vesper Bot Action Requires Approval*\n"
            f">{action_summary}\n"
            f"*Reasoning:* {reasoning}\n"
            f"<{approve_url}|✅ Approve> | <{reject_url}|❌ Reject>"
        )
        _send_slack(smtp["slack_webhook"], slack_msg)

    return sent


def send_weekly_digest(
    db: Session,
    company_id: str,
    period: Optional[str] = None,
) -> bool:
    """
    Send a weekly digest email summarizing KPIs, open proposals, and anomalies.
    """
    smtp = _get_smtp_config(db, company_id)
    if not smtp:
        return False

    # Gather data
    if not period:
        latest = db.query(InsightKPI.period).filter(
            InsightKPI.company_id == company_id
        ).order_by(InsightKPI.period.desc()).first()
        period = latest[0] if latest else "N/A"

    kpi_rows = db.query(InsightKPI).filter(
        InsightKPI.company_id == company_id,
        InsightKPI.period == period,
    ).all()
    kpis = {r.name: float(r.value or 0) for r in kpi_rows}

    pending_proposals = db.query(ProposedAction).filter(
        ProposedAction.company_id == company_id,
        ProposedAction.status == "PENDING",
    ).count()

    # AC-1 (plan 20260716-anomaly-floor-remaining-edges): the digest applies the
    # same soft-close anomaly floor as the Alerts Center (filter-only — rows are
    # never deleted/dismissed, so a break-glass re-open resurfaces them). Floor
    # None ⇒ unfiltered. A floor-read FAILURE fails CLOSED: the anomaly line is
    # OMITTED from the digest (open_anomalies stays None; NEVER rendered
    # unfiltered) and the email still sends — a calendar outage must not block
    # the digest or leak closed-period rows into it.
    open_anomalies: int | None = None
    try:
        anomaly_floor = latest_locked_period(db, realm_id=company_id, hard_only=False)
    except Exception as e:
        logger.error(
            "weekly digest anomaly floor read failed for company %s — omitting the "
            "anomaly section (fail-closed): %s",
            company_id,
            redact_to_string(e),
        )
    else:
        anomaly_q = db.query(InsightAnomaly).filter(
            InsightAnomaly.company_id == company_id,
            InsightAnomaly.dismissed == False,  # noqa: E712
        )
        if anomaly_floor is not None:
            anomaly_q = anomaly_q.filter(InsightAnomaly.period > anomaly_floor)
        open_anomalies = anomaly_q.count()

    def fmt_currency(v):
        try:
            return f"${float(v):,.0f}"
        except Exception:
            return "—"

    revenue = fmt_currency(kpis.get("revenue") or kpis.get("ttm_revenue"))
    ebitda = fmt_currency(kpis.get("ebitda"))
    cash = fmt_currency(kpis.get("cash") or kpis.get("cash_and_equivalents"))

    base = os.getenv("APP_BASE_URL", "http://localhost:3000")

    # AC-1 fail-closed rendering: when the floor read failed (open_anomalies is
    # None) the anomaly line is omitted entirely — an unfiltered count never
    # reaches the email.
    anomaly_item = (
        f"<p>⚠️ <strong>{open_anomalies}</strong> open anomaly/anomalies</p>"
        if open_anomalies is not None
        else ""
    )

    html = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
      <div style="background: #1e3a5f; padding: 24px; border-radius: 8px 8px 0 0;">
        <h1 style="color: white; margin: 0; font-size: 20px;">🦅 Vesper Bot Weekly Summary — {period}</h1>
      </div>
      <div style="border: 1px solid #e0e0e0; border-top: none; padding: 24px; border-radius: 0 0 8px 8px;">
        <h2 style="color: #1e3a5f; font-size: 16px;">Key Metrics</h2>
        <table style="width:100%; border-collapse: collapse; margin-bottom: 16px;">
          <tr style="background:#f0f4f8;"><td style="padding:8px;font-weight:bold;">Revenue (MTD)</td><td style="padding:8px;text-align:right;">{revenue}</td></tr>
          <tr><td style="padding:8px;font-weight:bold;">EBITDA</td><td style="padding:8px;text-align:right;">{ebitda}</td></tr>
          <tr style="background:#f0f4f8;"><td style="padding:8px;font-weight:bold;">Cash on Hand</td><td style="padding:8px;text-align:right;">{cash}</td></tr>
        </table>
        <h2 style="color: #1e3a5f; font-size: 16px;">Action Items</h2>
        <p>📋 <strong>{pending_proposals}</strong> proposal(s) awaiting approval</p>
        {anomaly_item}
        <div style="margin-top: 24px;">
          <a href="{base}/dashboard" style="background:#1e3a5f; color:white; padding:12px 24px; border-radius:6px; text-decoration:none; font-weight:bold;">
            Open Vesper Bot Dashboard →
          </a>
        </div>
      </div>
    </body></html>
    """

    subject = f"Vesper Bot Weekly Summary — {period}"
    return _send_email(smtp, subject, html)


def send_report_email(
    db: Session,
    company_id: str,
    period: str,
    recipients: List[str],
    attachments: List[Dict[str, Any]] = None,
    kpis: Optional[Dict[str, Any]] = None,
    schedule_name: str = "Financial Report",
) -> bool:
    """
    Send a financial report email with PDF/Excel attachments.

    Args:
        db: Database session
        company_id: QBO realm ID
        period: Report period (e.g. '2026-02')
        recipients: List of email addresses
        attachments: List of dicts with 'filename', 'content' (bytes), 'mime_type'
        kpis: Optional KPI dict for highlights in the email body
        schedule_name: Name of the schedule (for subject line)
    """
    smtp = _get_smtp_config(db, company_id)
    if not smtp:
        logger.info(f"No email config for company {company_id}; skipping report email")
        return False

    if not recipients:
        logger.warning(f"No recipients for report email — company {company_id}")
        return False

    # Build KPI highlights
    kpis = kpis or {}

    def fmt(v):
        try:
            return f"${float(v):,.0f}"
        except Exception:
            return "—"

    revenue = fmt(kpis.get("revenue"))
    net_income = fmt(kpis.get("net_income"))
    cash = fmt(kpis.get("cash_and_equivalents") or kpis.get("cash"))
    ebitda = fmt(kpis.get("ebitda"))

    attachment_list = ""
    for att in (attachments or []):
        icon = "📊" if "excel" in att.get("mime_type", "") else "📄"
        attachment_list += f'<li style="padding:4px 0;">{icon} {att["filename"]}</li>'

    base = os.getenv("APP_BASE_URL", "http://localhost:3000")

    html = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 640px; margin: 0 auto; padding: 20px;">
      <div style="background: linear-gradient(135deg, #1e3a5f, #2c5282); padding: 28px; border-radius: 10px 10px 0 0;">
        <h1 style="color: white; margin: 0; font-size: 22px;">🦅 Vesper Bot — {schedule_name}</h1>
        <p style="color: #a0c4e8; margin: 8px 0 0; font-size: 14px;">Period: {period}</p>
      </div>
      <div style="border: 1px solid #e0e0e0; border-top: none; padding: 28px; border-radius: 0 0 10px 10px;">
        <h2 style="color: #1e3a5f; font-size: 16px; margin-top: 0;">📊 Key Metrics</h2>
        <table style="width:100%; border-collapse: collapse; margin-bottom: 20px;">
          <tr style="background:#f0f4f8;">
            <td style="padding:10px;font-weight:bold;border-bottom:1px solid #e0e0e0;">Revenue</td>
            <td style="padding:10px;text-align:right;border-bottom:1px solid #e0e0e0;">{revenue}</td>
          </tr>
          <tr>
            <td style="padding:10px;font-weight:bold;border-bottom:1px solid #e0e0e0;">Net Income</td>
            <td style="padding:10px;text-align:right;border-bottom:1px solid #e0e0e0;">{net_income}</td>
          </tr>
          <tr style="background:#f0f4f8;">
            <td style="padding:10px;font-weight:bold;border-bottom:1px solid #e0e0e0;">EBITDA</td>
            <td style="padding:10px;text-align:right;border-bottom:1px solid #e0e0e0;">{ebitda}</td>
          </tr>
          <tr>
            <td style="padding:10px;font-weight:bold;">Cash on Hand</td>
            <td style="padding:10px;text-align:right;">{cash}</td>
          </tr>
        </table>

        <h2 style="color: #1e3a5f; font-size: 16px;">📎 Attached Reports</h2>
        <ul style="list-style:none; padding:0; margin:0 0 20px;">{attachment_list or '<li style="padding:4px 0;">No attachments</li>'}</ul>

        <div style="margin-top: 24px;">
          <a href="{base}/reports" style="background:#1e3a5f; color:white; padding:12px 28px; border-radius:6px; text-decoration:none; font-weight:bold;">
            View in Vesper Bot →
          </a>
        </div>

        <p style="margin-top: 24px; font-size: 12px; color: #999;">
          This report was automatically generated by Vesper Bot.
          You can manage your report schedules in
          <a href="{base}/reports" style="color: #1e3a5f;">Settings</a>.
        </p>
      </div>
    </body></html>
    """

    subject = f"Vesper Bot: {schedule_name} — {period}"

    # Send to each recipient
    sent_count = 0
    for recipient in recipients:
        try:
            msg = MIMEMultipart("mixed")
            msg["Subject"] = subject
            msg["From"] = smtp["user"] or "vesper-bot@example.invalid"
            msg["To"] = recipient

            # HTML body
            html_part = MIMEMultipart("alternative")
            html_part.attach(MIMEText(html, "html"))
            msg.attach(html_part)

            # File attachments
            for att in (attachments or []):
                part = MIMEBase("application", "octet-stream")
                part.set_payload(att["content"])
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    f'attachment; filename="{att["filename"]}"',
                )
                msg.attach(part)

            with smtplib.SMTP(smtp["host"], smtp["port"]) as server:
                server.ehlo()
                server.starttls()
                if smtp["user"] and smtp["password"]:
                    server.login(smtp["user"], smtp["password"])
                server.sendmail(msg["From"], recipient, msg.as_string())

            sent_count += 1
            logger.info(f"Report email sent to {recipient}: {subject}")

        except Exception as e:
            logger.error(f"Failed to send report email to {recipient}: {e}")

    # Slack notification
    if smtp.get("slack_webhook") and sent_count > 0:
        att_names = ", ".join(a["filename"] for a in (attachments or []))
        slack_msg = (
            f"🦅 *Vesper Bot Report Delivered*\n"
            f"> *{schedule_name}* for {period}\n"
            f"> Sent to {sent_count} recipient(s)\n"
            f"> Attachments: {att_names or 'None'}"
        )
        _send_slack(smtp["slack_webhook"], slack_msg)

    return sent_count > 0


# ─────────────────────────────────────────────────────────────────────────────
# Morning Briefing Email
# ─────────────────────────────────────────────────────────────────────────────

def _send_morning_briefing_transport(
    db: Session,
    company_id: str,
    company_name: str,
    briefing,
    *, smtp, message_id,
) -> SMTPDeliveryResult:
    """
    Send the daily morning briefing email.
    Uses the ExecutiveBriefing data to build a branded HTML email.

    Args:
        db: Database session
        company_id: Realm ID
        company_name: Human-readable company name
        briefing: ExecutiveBriefing model instance
    """
    def fmt(v):
        if v is None:
            return "—"
        try:
            num = float(v)
            if abs(num) >= 1_000_000:
                return f"${num/1_000_000:,.1f}M"
            elif abs(num) >= 1_000:
                return f"${num/1_000:,.1f}K"
            return f"${num:,.0f}"
        except (ValueError, TypeError):
            return str(v)

    # Extract data from briefing
    fin = briefing.financial_position or {}
    risks = briefing.key_risks or []
    pending_count = briefing.pending_actions_count or 0
    anomaly_count = briefing.open_anomalies_count or 0
    narrative = briefing.narrative or ""
    briefing_date = briefing.briefing_date

    # Financial position KPIs
    revenue = fin.get("revenue", {})
    net_income = fin.get("net_income", {})
    cash = fin.get("cash", {})

    rev_val = fmt(revenue.get("value"))
    ni_val = fmt(net_income.get("value"))
    cash_val = fmt(cash.get("value"))

    rev_delta = revenue.get("delta")
    ni_delta = net_income.get("delta")

    def delta_badge(delta):
        if delta is None:
            return ""
        try:
            d = float(delta)
            color = "#10b981" if d >= 0 else "#ef4444"
            arrow = "▲" if d >= 0 else "▼"
            return f'<span style="color:{color};font-size:12px;margin-left:4px">{arrow} {abs(d):.1f}%</span>'
        except (ValueError, TypeError):
            return ""

    # Risk alerts HTML
    risk_html = ""
    if risks:
        risk_rows = ""
        for r in risks[:5]:
            severity = r.get("severity", "warning")
            badge_color = "#ef4444" if severity == "critical" else "#f59e0b"
            risk_rows += f"""
            <tr>
                <td style="padding:6px 10px;border-bottom:1px solid #f0f0f0">
                    <span style="background:{badge_color};color:white;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:600">{severity.upper()}</span>
                </td>
                <td style="padding:6px 10px;border-bottom:1px solid #f0f0f0;color:#374151;font-size:13px">{r.get('description', '')}</td>
            </tr>"""
        risk_html = f"""
        <div style="margin:20px 0">
            <h3 style="color:#1e293b;font-size:15px;margin-bottom:8px">⚠️ Active Risk Alerts ({len(risks)})</h3>
            <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:6px;overflow:hidden;border:1px solid #e5e7eb">
                {risk_rows}
            </table>
        </div>"""

    # Pending actions
    actions_html = ""
    if pending_count > 0 or anomaly_count > 0:
        actions_html = f"""
        <div style="margin:20px 0;display:flex;gap:12px">
            <div style="flex:1;background:#fef3c7;border-radius:8px;padding:12px 16px;text-align:center">
                <div style="font-size:24px;font-weight:700;color:#92400e">{pending_count}</div>
                <div style="font-size:12px;color:#92400e">Pending Actions</div>
            </div>
            <div style="flex:1;background:#fee2e2;border-radius:8px;padding:12px 16px;text-align:center">
                <div style="font-size:24px;font-weight:700;color:#991b1b">{anomaly_count}</div>
                <div style="font-size:12px;color:#991b1b">Open Anomalies</div>
            </div>
        </div>"""

    html = f"""
    <div style="font-family:'Segoe UI',Roboto,sans-serif;max-width:600px;margin:0 auto;background:#f8fafc;padding:24px">
        <!-- Header -->
        <div style="background:linear-gradient(135deg,#1e293b 0%,#334155 100%);color:white;padding:24px;border-radius:12px 12px 0 0">
            <div style="font-size:12px;text-transform:uppercase;letter-spacing:1px;opacity:0.7;margin-bottom:4px">Vesper Bot</div>
            <h1 style="margin:0;font-size:22px;font-weight:600">Morning Briefing</h1>
            <div style="font-size:13px;opacity:0.8;margin-top:4px">{company_name} — {briefing_date}</div>
        </div>

        <!-- Financial Position -->
        <div style="background:white;padding:20px;border:1px solid #e2e8f0">
            <h3 style="color:#1e293b;font-size:15px;margin:0 0 12px 0">📊 Financial Position</h3>
            <div style="display:flex;gap:12px">
                <div style="flex:1;background:#f0fdf4;border-radius:8px;padding:12px 16px;text-align:center">
                    <div style="font-size:11px;color:#6b7280;margin-bottom:4px">Revenue</div>
                    <div style="font-size:20px;font-weight:700;color:#166534">{rev_val}{delta_badge(rev_delta)}</div>
                </div>
                <div style="flex:1;background:#eff6ff;border-radius:8px;padding:12px 16px;text-align:center">
                    <div style="font-size:11px;color:#6b7280;margin-bottom:4px">Net Income</div>
                    <div style="font-size:20px;font-weight:700;color:#1e40af">{ni_val}{delta_badge(ni_delta)}</div>
                </div>
                <div style="flex:1;background:#faf5ff;border-radius:8px;padding:12px 16px;text-align:center">
                    <div style="font-size:11px;color:#6b7280;margin-bottom:4px">Cash</div>
                    <div style="font-size:20px;font-weight:700;color:#7c3aed">{cash_val}</div>
                </div>
            </div>

            {risk_html}
            {actions_html}
        </div>

        <!-- Narrative -->
        <div style="background:white;padding:20px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px">
            <h3 style="color:#1e293b;font-size:15px;margin:0 0 8px 0">📝 Summary</h3>
            <p style="color:#4b5563;font-size:13px;line-height:1.6;margin:0;white-space:pre-line">{narrative}</p>
        </div>

        <!-- Footer -->
        <div style="text-align:center;margin-top:16px;color:#9ca3af;font-size:11px">
            Vesper Bot — AI-Powered Financial Intelligence<br>
            Generated at {briefing.data_freshness_at.strftime('%I:%M %p UTC') if briefing.data_freshness_at else 'N/A'}
        </div>
    </div>
    """

    subject = f"🦅 Morning Briefing — {company_name} ({briefing_date})"
    return _send_email_detailed(smtp, subject, html, message_id=message_id)


def send_morning_briefing(db: Session, company_id: str, company_name: str, briefing) -> dict:
    """Both automatic callers share the same daily delivery identity."""
    from .briefing_delivery import deliver_briefing
    smtp = _get_smtp_config(db, company_id)
    if not smtp:
        return {"status": "not_configured", "reason": "smtp_not_configured"}
    # Claims commit and expire ORM rows. Snapshot render inputs before that
    # boundary so rendering cannot reopen a DB transaction through SMTP.
    from types import SimpleNamespace
    from copy import deepcopy
    rendered = SimpleNamespace(**{name: deepcopy(getattr(briefing, name)) for name in (
        "financial_position", "key_risks", "pending_actions_count", "open_anomalies_count",
        "narrative", "briefing_date", "data_freshness_at",
    )})
    return deliver_briefing(db, company_id=company_id, briefing=briefing, smtp=smtp,
        send=lambda message_id: _send_morning_briefing_transport(
            db, company_id, company_name, rendered, smtp=smtp, message_id=message_id))
