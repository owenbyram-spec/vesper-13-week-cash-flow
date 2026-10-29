from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Literal, Optional
from pydantic import BaseModel
import logging
from uuid import UUID

from ..db import get_db
from ..models import CashForecastExportSnapshot, User, Report, ReportSchedule
from ..cashflow.export_snapshots import (
    ExportSnapshotContextMismatchError,
    ExportSnapshotIntegrityError,
    ExportSnapshotNotFoundError,
    ExportSnapshotNotReadyError,
    ExportSnapshotSchemaUnsupportedError,
    load_forecast_export_from_authorized_row,
)
from ..dependencies import check_entity_tenant_access, check_tenant_access, get_current_user_from_token
from ..reporting.named_report_builders import cashflow_report_payload_from_export
from ..reporting.report_builder import build_report_payload
from ..reporting.pdf_renderer import render_pdf
from ..reporting.excel_renderer import render_excel
from ..reporting.report_definitions import get_report_definition
from ..services.auth_migration import record_auth_event
from ..services.errors import user_safe_http_exception
from ..services.redaction import redact_to_string
from ..services.tenant_companies import organization_for_company

router = APIRouter(prefix="/reporting", tags=["Reporting"])
logger = logging.getLogger(__name__)


def _audit_report_event(
    db: Session,
    *,
    event_type: str,
    user: User,
    company_id: str,
    period: str,
    report_format: Optional[str] = None,
) -> None:
    record_auth_event(
        db,
        event_type=event_type,
        user=user,
        organization=organization_for_company(db, company_id),
        details={
            "realm_id": company_id,
            "period": period,
            "format": report_format,
        },
    )


def _record_report_failure(
    db: Session,
    *,
    event_type: str,
    user: User,
    company_id: str,
    period: str,
    report_format: str,
) -> None:
    try:
        db.rollback()
        _audit_report_event(
            db,
            event_type=event_type,
            user=user,
            company_id=company_id,
            period=period,
            report_format=report_format,
        )
        db.commit()
    except Exception as audit_exc:
        db.rollback()
        logger.warning(
            "report_failure_audit_failed company_id=%s period=%s error=%s",
            company_id,
            period,
            redact_to_string(str(audit_exc), max_length=500),
        )


def _cashflow_export_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Cash-flow forecast export not found")


def _normalize_cashflow_export_denial(exc: HTTPException) -> None:
    """Hide unknown and unauthorized exact IDs behind one 404 response."""
    if exc.status_code in {403, 404}:
        raise _cashflow_export_not_found() from exc
    raise exc


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class ScheduleCreate(BaseModel):
    company_id: str
    name: str                              # "Monthly Board Pack"
    frequency: str                         # WEEKLY | MONTHLY | QUARTERLY
    report_format: str = "pdf"             # pdf | excel | both
    recipients: List[str]                  # ["user@example.com"]
    day_of_week: Optional[int] = None      # 0-6 for WEEKLY
    day_of_month: Optional[int] = None     # 1-28 for MONTHLY/QUARTERLY
    hour_utc: int = 8                      # 0-23
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    name: Optional[str] = None
    frequency: Optional[str] = None
    report_format: Optional[str] = None
    recipients: Optional[List[str]] = None
    day_of_week: Optional[int] = None
    day_of_month: Optional[int] = None
    hour_utc: Optional[int] = None
    enabled: Optional[bool] = None

@router.get("", response_model=List[Dict[str, Any]])
def list_reports(
    company_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    reports = db.query(Report).filter(Report.company_id == company_id).order_by(Report.generated_at.desc()).all()
    return [
        {
            "id": r.id,
            "company_id": r.company_id,
            "report_period": r.period,
            "generated_at": r.generated_at.isoformat() if r.generated_at else None,
            "s3_key": f"/reporting/{r.company_id}/{r.period}/download",
            "report_type": r.report_type,
        }
        for r in reports
    ]


@router.post("/generate")
def generate_report(
    company_id: str = Query(...),
    period: str = Query(...),
    report_type: str = Query("month_end", description="Report definition to generate (default: month_end)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Triggers synchronous report generation for the given company and period.
    Builds the report data from live QBO, renders a stub PDF, and stores
    a Report record so it appears in the history list.
    """
    try:
        check_tenant_access("EDITOR", company_id=company_id, db=db, user=current_user)
        payload = build_report_payload(db, company_id, period, report_type)
        pdf_bytes = render_pdf(payload, get_report_definition(report_type))  # noqa: F841

        # Upsert report record
        existing = db.query(Report).filter(
            Report.company_id == company_id,
            Report.period == period,
        ).first()

        if existing:
            # Re-generate: update timestamp
            from sqlalchemy.sql import func
            existing.generated_at = func.now()
            existing.file_path = "regenerated"
            existing.report_type = report_type
        else:
            report = Report(
                company_id=company_id,
                period=period,
                file_path="generated_on_demand",
                report_type=report_type,
                generated_by="user",
            )
            # LAYER-OK: pre-existing inline-db persistence in this legacy router (unchanged by AC-2; module-wide pattern)
            db.add(report)

        db.commit()
        _audit_report_event(
            db,
            event_type="report_generated",
            user=current_user,
            company_id=company_id,
            period=period,
            report_format="pdf",
        )
        db.commit()

        return {"status": "ok", "period": period, "message": f"Report for {period} generated successfully."}
    except ExportSnapshotNotReadyError as e:
        raise user_safe_http_exception(
            status_code=409,
            error_code="cashflow_export_not_ready",
            user_message=(
                "The cash-flow forecast is still preparing its downloadable snapshot. "
                "Retry after the forecast finishes loading."
            ),
            exc=e,
            retryable=True,
            log_context="cashflow_export_not_ready",
        ) from e
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        _record_report_failure(
            db,
            event_type="report_generation_failed",
            user=current_user,
            company_id=company_id,
            period=period,
            report_format="pdf",
        )
        raise user_safe_http_exception(
            status_code=500,
            error_code="report_generation_failed",
            user_message="The report could not be generated. Please try again.",
            exc=e,
            retryable=True,
            log_context="report_generation_failed",
        )


@router.get("/cashflow-forecast/exports/{export_id}/download")
def download_cashflow_forecast_export(
    export_id: str,
    expected_company_id: str = Query(...),
    format: Literal["excel", "pdf"] = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Render one immutable CFF snapshot without rebuilding or rereading QBO.

    ``expected_company_id`` is authorized independently from the stored owner.  A
    caller who cannot see either side receives the same 404 as an unknown ID; a
    mismatch is disclosed only when the caller is authorized for both companies.
    """
    try:
        normalized_export_id = UUID(str(export_id))
    except (TypeError, ValueError) as exc:
        raise _cashflow_export_not_found() from exc

    try:
        check_tenant_access(
            "VIEWER",
            company_id=expected_company_id,
            db=db,
            user=current_user,
        )
    except HTTPException as exc:
        _normalize_cashflow_export_denial(exc)

    expected_organization = organization_for_company(db, expected_company_id)
    if expected_organization is None:
        raise _cashflow_export_not_found()

    try:
        row = check_entity_tenant_access(
            "VIEWER",
            db=db,
            user=current_user,
            model=CashForecastExportSnapshot,
            entity_id=normalized_export_id,
            not_found_detail="Cash-flow forecast export not found",
        )
    except HTTPException as exc:
        _normalize_cashflow_export_denial(exc)

    if str(row.company_id) != str(expected_company_id):
        raise user_safe_http_exception(
            status_code=409,
            error_code="cashflow_export_context_mismatch",
            user_message=(
                "The selected company changed before the forecast download began. "
                "Return to the forecast and retry the download."
            ),
            retryable=True,
            log_context="cashflow_export_context_mismatch",
        )

    try:
        export = load_forecast_export_from_authorized_row(
            row,
            expected_company_id=expected_company_id,
            expected_organization_id=expected_organization.id,
        )
    except ExportSnapshotNotFoundError as exc:
        raise _cashflow_export_not_found() from exc
    except ExportSnapshotContextMismatchError as exc:
        raise user_safe_http_exception(
            status_code=409,
            error_code="cashflow_export_context_mismatch",
            user_message=(
                "The selected company changed before the forecast download began. "
                "Return to the forecast and retry the download."
            ),
            exc=exc,
            retryable=True,
            log_context="cashflow_export_context_mismatch",
        ) from exc
    except ExportSnapshotSchemaUnsupportedError as exc:
        raise user_safe_http_exception(
            status_code=409,
            error_code="cashflow_export_schema_unsupported",
            user_message=(
                "This forecast snapshot uses a newer export format. Refresh the forecast "
                "and retry the download."
            ),
            exc=exc,
            retryable=True,
            log_context="cashflow_export_schema_unsupported",
        ) from exc
    except ExportSnapshotIntegrityError as exc:
        raise user_safe_http_exception(
            status_code=500,
            error_code="cashflow_export_integrity_failed",
            user_message=(
                "The saved forecast snapshot could not be verified, so no file was created. "
                "Refresh the forecast and retry."
            ),
            exc=exc,
            retryable=True,
            log_context="cashflow_export_integrity_failed",
        ) from exc

    payload = cashflow_report_payload_from_export(export)
    definition = get_report_definition("cashflow_forecast")
    filename_base = f"cashflow_forecast_{export.company_id}_{export.as_of_week_ending.isoformat()}"
    try:
        if format == "excel":
            content = render_excel(payload, definition)
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            extension = "xlsx"
        else:
            content = render_pdf(payload, definition)
            media_type = "application/pdf"
            extension = "pdf"
    except Exception as exc:  # noqa: BLE001 — renderer errors must never return partial files
        raise user_safe_http_exception(
            status_code=500,
            error_code="cashflow_export_render_failed",
            user_message="The forecast file could not be rendered. Please retry the download.",
            exc=exc,
            retryable=True,
            log_context="cashflow_export_render_failed",
        ) from exc

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename_base}.{extension}"',
        },
    )


@router.get("/{company_id}/{period}/download")
def download_report(
    company_id: str,
    period: str, # e.g. 2024-12
    format: str = Query("pdf", description="Output format: 'pdf' or 'excel'"),
    report_type: str = Query("month_end", description="Report definition to render (default: month_end)"),
    view: Optional[str] = Query(
        None,
        description=(
            "Statement window for standard_financials: 'ytd' (default) | 'ttm' | 'month'. "
            "YTD/TTM render monthly columns (P&L/CF also carry QBO's Total column; the "
            "balance sheet is point-in-time month-end columns with no total)."
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Download the financial report for a given company and period.
    Supports both PDF and Excel formats via the `format` query parameter,
    and any named report via `report_type` (defaults to month_end).
    """
    try:
        check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
        # Build the per-report_type payload + resolve its definition (same data for both
        # formats). `view` is forwarded only to builders that declare it (W-4 fix #7).
        # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4
        payload = build_report_payload(db, company_id, period, report_type, view=view)
        definition = get_report_definition(report_type)

        if format.lower() == "excel":
            excel_bytes = render_excel(payload, definition)
            return Response(
                content=excel_bytes,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={
                    "Content-Disposition": f'attachment; filename="{report_type}_{company_id}_{period}.xlsx"'
                }
            )
        else:
            pdf_bytes = render_pdf(payload, definition)
            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'attachment; filename="{report_type}_{company_id}_{period}.pdf"'
                }
            )
        
    except ExportSnapshotNotReadyError as e:
        raise user_safe_http_exception(
            status_code=409,
            error_code="cashflow_export_not_ready",
            user_message=(
                "The cash-flow forecast is still preparing its downloadable snapshot. "
                "Retry after the forecast finishes loading."
            ),
            exc=e,
            retryable=True,
            log_context="cashflow_export_not_ready",
        ) from e
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise user_safe_http_exception(
            status_code=500,
            error_code="report_export_failed",
            user_message="The report export could not be prepared. Please try again.",
            exc=e,
            retryable=True,
            log_context="report_export_failed",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Report Schedule CRUD
# ─────────────────────────────────────────────────────────────────────────────

def _schedule_to_dict(s: ReportSchedule) -> Dict[str, Any]:
    return {
        "id": s.id,
        "company_id": s.company_id,
        "name": s.name,
        "frequency": s.frequency,
        "report_format": s.report_format,
        "recipients": s.recipients or [],
        "enabled": s.enabled,
        "day_of_week": s.day_of_week,
        "day_of_month": s.day_of_month,
        "hour_utc": s.hour_utc,
        "last_sent_at": s.last_sent_at.isoformat() if s.last_sent_at else None,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/schedules", response_model=List[Dict[str, Any]])
def list_schedules(
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """List all report schedules for a company."""
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    schedules = db.query(ReportSchedule).filter(
        ReportSchedule.company_id == company_id,
    ).order_by(ReportSchedule.created_at.desc()).all()
    return [_schedule_to_dict(s) for s in schedules]


@router.post("/schedules")
def create_schedule(
    body: ScheduleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Create a new report schedule."""
    check_tenant_access("EDITOR", company_id=body.company_id, db=db, user=current_user)
    # Validate frequency-specific fields
    if body.frequency == "WEEKLY" and body.day_of_week is None:
        raise HTTPException(400, "day_of_week is required for WEEKLY schedules (0=Mon..6=Sun)")
    if body.frequency in ("MONTHLY", "QUARTERLY") and body.day_of_month is None:
        raise HTTPException(400, "day_of_month is required for MONTHLY/QUARTERLY schedules (1-28)")

    schedule = ReportSchedule(
        company_id=body.company_id,
        name=body.name,
        frequency=body.frequency.upper(),
        report_format=body.report_format.lower(),
        recipients=body.recipients,
        day_of_week=body.day_of_week,
        day_of_month=body.day_of_month,
        hour_utc=body.hour_utc,
        enabled=body.enabled,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return _schedule_to_dict(schedule)


@router.patch("/schedules/{schedule_id}")
def update_schedule(
    schedule_id: int,
    body: ScheduleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Update an existing report schedule."""
    schedule = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=ReportSchedule,
        entity_id=schedule_id,
        not_found_detail="Schedule not found",
    )

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "frequency" and value:
            value = value.upper()
        if field == "report_format" and value:
            value = value.lower()
        setattr(schedule, field, value)

    db.commit()
    db.refresh(schedule)
    return _schedule_to_dict(schedule)


@router.delete("/schedules/{schedule_id}")
def delete_schedule(
    schedule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Delete a report schedule."""
    schedule = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=ReportSchedule,
        entity_id=schedule_id,
        not_found_detail="Schedule not found",
    )

    db.delete(schedule)
    db.commit()
    return {"status": "deleted", "id": schedule_id}
