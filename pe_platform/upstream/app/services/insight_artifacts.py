from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import InsightArtifact


def create_insight_artifact(
    db: Session,
    *,
    company_id: str,
    title: str,
    summary: str,
    severity: str = "info",
    metric: Optional[str] = None,
    period: Optional[str] = None,
    source_refs: Optional[List[Dict[str, Any]]] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    recommendations: Optional[List[Dict[str, Any]]] = None,
    confidence: Optional[float] = None,
    agent_run_id: Optional[int] = None,
    data_freshness_at: Optional[datetime] = None,
    organization_id: Optional[int] = None,
    organization_company_id: Optional[int] = None,
) -> InsightArtifact:
    artifact = InsightArtifact(
        organization_id=organization_id,
        organization_company_id=organization_company_id,
        company_id=company_id,
        agent_run_id=agent_run_id,
        title=title,
        summary=summary,
        severity=severity,
        metric=metric,
        period=period,
        source_refs=source_refs or [],
        evidence=evidence or [],
        recommendations=recommendations or [],
        confidence=Decimal(str(confidence)) if confidence is not None else None,
        data_freshness_at=data_freshness_at or datetime.utcnow(),
        status="open",
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    return artifact


def objective_result_to_artifact(
    db: Session,
    *,
    objective_type: str,
    company_id: str,
    result: Dict[str, Any],
    agent_run_id: Optional[int],
    organization_id: Optional[int] = None,
    organization_company_id: Optional[int] = None,
) -> Optional[InsightArtifact]:
    if objective_type == "morning_briefing":
        return create_insight_artifact(
            db,
            company_id=company_id,
            agent_run_id=agent_run_id,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            title="Morning briefing generated",
            summary=f"Executive briefing prepared for {result.get('period', 'latest period')}.",
            severity="info",
            metric="executive_briefing",
            period=result.get("period"),
            source_refs=[{"type": "executive_briefing", "period": result.get("period")}],
            evidence=[
                {"label": "Company", "value": result.get("company_name")},
                {"label": "Email sent", "value": result.get("email_sent")},
            ],
            recommendations=[{"action": "Review pending actions and open anomalies.", "confidence": 0.75}],
            confidence=0.85,
        )

    if objective_type == "cash_watch":
        negative_weeks = result.get("negative_cash_weeks") or []
        severity = "critical" if negative_weeks else "info"
        return create_insight_artifact(
            db,
            company_id=company_id,
            agent_run_id=agent_run_id,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            title="Cash watch completed" if not negative_weeks else "Cash watch found negative cash risk",
            summary=(
                f"13-week forecast runway: {result.get('runway_weeks', 'unknown')} week(s). "
                f"Negative cash weeks: {len(negative_weeks)}."
            ),
            severity=severity,
            metric="cash_forecast",
            source_refs=[{"type": "cash_forecast", "horizon": "13_weeks"}],
            evidence=[
                {"label": "Runway weeks", "value": result.get("runway_weeks")},
                {"label": "Lowest balance", "value": result.get("lowest_balance")},
                {"label": "Negative weeks", "value": negative_weeks},
            ],
            recommendations=[{"action": "Prioritize collections and defer noncritical payments if cash dips below target.", "confidence": 0.8}],
            confidence=0.82,
        )

    if objective_type == "budget_variance_watch":
        return create_insight_artifact(
            db,
            company_id=company_id,
            agent_run_id=agent_run_id,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            title="Budget variance watch completed",
            summary=f"Vendor-level budget variance scan completed for {result.get('period')}.",
            severity="info",
            metric="budget_variance",
            period=result.get("period"),
            source_refs=[{"type": "budget_lines"}, {"type": "qbo_vendor_spend", "period": result.get("period")}],
            evidence=[{"label": "Status", "value": result.get("status")}],
            recommendations=[{"action": "Review any generated budget update proposals in Actions.", "confidence": 0.78}],
            confidence=0.8,
        )

    if objective_type == "qbo_sync_health":
        severity = "warning" if result.get("status") == "attention_needed" else "info"
        return create_insight_artifact(
            db,
            company_id=company_id,
            agent_run_id=agent_run_id,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            title="QuickBooks sync health checked",
            summary=f"QBO refresh token has {result.get('refresh_days_remaining')} day(s) remaining.",
            severity=severity,
            metric="qbo_sync_health",
            source_refs=[{"type": "qbo_token"}],
            evidence=[
                {"label": "Access minutes remaining", "value": result.get("access_minutes_remaining")},
                {"label": "Refresh days remaining", "value": result.get("refresh_days_remaining")},
            ],
            recommendations=[{"action": "Reconnect QuickBooks before token expiry if days remaining are low.", "confidence": 0.9}],
            confidence=0.9,
        )

    if objective_type == "report_pack":
        return create_insight_artifact(
            db,
            company_id=company_id,
            agent_run_id=agent_run_id,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            title="Report pack generated",
            summary=f"Report pack generated for {result.get('period')} with {len(result.get('attachments', []))} attachment(s).",
            severity="info",
            metric="report_pack",
            period=result.get("period"),
            source_refs=[{"type": "financial_report", "period": result.get("period")}],
            evidence=[
                {"label": "Format", "value": result.get("format")},
                {"label": "Attachments", "value": result.get("attachments")},
                {"label": "Email sent", "value": result.get("email_sent")},
            ],
            recommendations=[{"action": "Review the generated report and share with stakeholders as needed.", "confidence": 0.75}],
            confidence=0.83,
        )

    if objective_type == "transaction_review":
        uncategorized = int(result.get("uncategorized_count") or 0)
        categorized = int(result.get("categorized_count") or 0)
        excluded = int(result.get("excluded_count") or 0)
        severity = "warning" if uncategorized else "info"
        title = "Transactions ready for classification" if uncategorized else "Transaction review prepared"
        return create_insight_artifact(
            db,
            company_id=company_id,
            agent_run_id=agent_run_id,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            title=title,
            summary=(
                f"Pulled {result.get('count', 0)} transaction(s) for week ending {result.get('week_ending')}. "
                f"{uncategorized} still need classification."
            ),
            severity=severity,
            metric="transaction_review",
            period=result.get("week_ending"),
            source_refs=[
                {"type": "cash_forecast_actuals", "week_ending": result.get("week_ending")},
                {"type": "cashflow_forecast_module"},
            ],
            evidence=[
                {"label": "Week ending", "value": result.get("week_ending")},
                {"label": "Transactions pulled", "value": result.get("count")},
                {"label": "Auto-categorized", "value": categorized},
                {"label": "Needs classification", "value": uncategorized},
                {"label": "Excluded internal transfers", "value": excluded},
            ],
            recommendations=[
                {"action": "Open Cash Flow Forecast and review the pulled transactions for classification.", "confidence": 0.92},
                {"action": "Ask the agent to help classify uncategorized transactions by vendor, customer, or transfer behavior.", "confidence": 0.88},
            ],
            confidence=0.9,
        )

    # PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections C4 / AC-3 (marker
    # RATIFICATION-2026-07-13.md on disk; dispatched session lacks active-plan state).
    if objective_type == "collections_review":
        chase_list = result.get("chase_list") or []
        missing_signals = result.get("missing_signals") or []
        drafted = result.get("drafted") or []
        skipped_existing = int(result.get("skipped_existing") or 0)
        candidate_count = int(result.get("candidate_count") or 0)

        severity = "warning" if candidate_count else "info"
        title = (
            "AR collections review: overdue invoices prioritized"
            if candidate_count
            else "AR collections review: no overdue invoices"
        )

        summary_parts = [
            f"{candidate_count} overdue invoice(s) scored; "
            f"total overdue balance {result.get('total_overdue_balance', 0)}."
        ]
        if drafted:
            summary_parts.append(
                f"{len(drafted)} invoice-reminder proposal(s) drafted for approval — nothing was sent."
            )
        elif candidate_count:
            summary_parts.append("No new reminder proposals drafted this run.")
        if skipped_existing:
            summary_parts.append(
                f"{skipped_existing} draft(s) skipped — an identical PENDING proposal already exists."
            )
        if missing_signals:
            summary_parts.append(
                "Signals unavailable this run: "
                + ", ".join(missing_signals)
                + " — prioritization proceeded WITHOUT them (their score components are zero, not estimated)."
            )

        evidence = [
            {"label": "Overdue invoices scored", "value": candidate_count},
            {"label": "Total overdue balance", "value": result.get("total_overdue_balance")},
            {
                "label": "Chase list (top priorities)",
                "value": [
                    {
                        "invoice_id": entry.get("invoice_id"),
                        "doc_number": entry.get("doc_number"),
                        "customer": entry.get("customer"),
                        "balance": entry.get("balance"),
                        "days_overdue": entry.get("days_overdue"),
                        "score": entry.get("score"),
                    }
                    for entry in chase_list[:10]
                ],
            },
            {"label": "Reminder proposals drafted", "value": drafted},
        ]
        if skipped_existing:
            evidence.append({"label": "Skipped (already pending)", "value": skipped_existing})
        if missing_signals:
            evidence.append({"label": "Missing signals", "value": missing_signals})

        if drafted:
            recommendations = [
                {
                    "action": "Review and approve (or reject) the drafted invoice reminders in Actions — nothing sends without approval.",
                    "confidence": 0.9,
                }
            ]
        elif candidate_count:
            recommendations = [
                {
                    "action": "Review the prioritized chase list; reminders for these invoices are already pending or capped this run.",
                    "confidence": 0.8,
                }
            ]
        else:
            recommendations = [
                {"action": "No overdue receivables — no collections action needed.", "confidence": 0.85}
            ]

        return create_insight_artifact(
            db,
            company_id=company_id,
            agent_run_id=agent_run_id,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            title=title,
            summary=" ".join(summary_parts),
            severity=severity,
            metric="collections_review",
            period=result.get("as_of"),
            source_refs=[
                {"type": "open_invoices"},
                {"type": "ar_aging", "as_of": result.get("as_of")},
                {"type": "payment_lateness"},
                {"type": "collection_haircuts"},
            ],
            evidence=evidence,
            recommendations=recommendations,
            # Confidence reflects the evidence actually present (degraded runs are labeled).
            confidence=0.85 if not missing_signals else 0.6,
        )

    # Wave 3.4 (plan 20260714-wave3-4-tax-pbc-binder AC-1 / WF-8): the tax-package
    # HANDOFF artifact. HARD requirement — an unknown type returns None → the in-app
    # persist silently no-ops. E1 stamps ride the payload; E2: confidence stays None
    # (a tax handoff must NOT read as an assurance score — the report_pack 0.83 trap).
    if objective_type == "tax_package":
        sections = result.get("sections") or []
        available = [s for s in sections if isinstance(s, dict) and s.get("status") == "OK"]
        period = result.get("period") or {}
        current = period.get("current") or {}
        period_label = current.get("label") or "the current fiscal year"
        return create_insight_artifact(
            db,
            company_id=company_id,
            agent_run_id=agent_run_id,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            title="Tax package assembled for preparer handoff",
            summary=(
                f"Tax package assembled for {period_label}: "
                f"{len(available)} of {len(sections)} section(s) available. "
                "Book-side handoff only — requires licensed-preparer review; no tax computed."
            ),
            severity="info",
            metric="tax_package",
            period=current.get("end_date"),
            source_refs=result.get("source_refs") or [{"type": "tax_package"}],
            evidence=[
                {"label": "Requires preparer review", "value": result.get("requires_preparer_review")},
                {"label": "Preparer review notice", "value": result.get("notice")},
                {"label": "Provenance", "value": result.get("provenance")},
                {"label": "Sections", "value": sections},
            ],
            recommendations=[
                {
                    "action": (
                        "Hand this package to your licensed tax preparer; the books-to-tax "
                        "mapping is an UNMAPPED scaffold for their review."
                    ),
                    "confidence": 0.9,
                }
            ],
            # E2: leave the artifact confidence NULL — a preparer handoff must not
            # read as an assurance score. requires_preparer_review carries the signal.
            confidence=None,
        )

    # Wave 3.4 (plan 20260714-wave3-4-tax-pbc-binder AC-2 / WF-9): the PBC
    # evidence-binder artifact. HARD requirement — an unknown type returns None → the
    # in-app persist silently no-ops. E2: confidence stays None (an evidence binder is
    # an ASSEMBLY, NOT an assurance score — the same trap the tax_package / report_pack
    # branches avoid); requires_human_review carries the signal.
    if objective_type == "pbc_binder":
        binders = result.get("binders") or []
        totals = result.get("totals") or {}
        total_lines = int(totals.get("lines") or 0)
        lines_linked = int(totals.get("lines_linked") or 0)
        return create_insight_artifact(
            db,
            company_id=company_id,
            agent_run_id=agent_run_id,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            title="PBC evidence binder assembled",
            summary=(
                f"Assembled evidence binders for {len(binders)} PBC request(s): "
                f"{lines_linked} of {total_lines} line(s) have supporting same-tenant "
                "evidence linked; the rest stay OPEN — never auto-satisfied."
            ),
            severity="info",
            metric="pbc_binder",
            source_refs=result.get("source_refs") or [{"type": "pbc_binder"}],
            evidence=[
                {"label": "Requires human review", "value": result.get("requires_human_review")},
                {"label": "Assemble-not-judge notice", "value": result.get("notice")},
                {"label": "Provenance", "value": result.get("provenance")},
                {"label": "PBC requests", "value": len(binders)},
                {"label": "Lines with linked evidence", "value": lines_linked},
                {"label": "Total lines", "value": total_lines},
                {"label": "Binders", "value": binders},
            ],
            recommendations=[
                {
                    "action": (
                        "Review each PBC line's linked evidence; a line marked NO_DATA "
                        "needs real supporting evidence attached before it can be provided."
                    ),
                    "confidence": 0.9,
                }
            ],
            # E2: leave the artifact confidence NULL — an evidence binder is an
            # assembly, not an assurance score.
            confidence=None,
        )

    return None
