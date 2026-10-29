"""Excel sections for the canonical cash-flow forecast export grid.

The forecast service/DTO owns every value and the row order.  This module only
writes that ordered projection to cells.  The small legacy adapter is retained
for board-pack payloads captured before the export DTO existed; it does not
calculate totals or balances.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .excel_context import ExcelContext

Payload = dict[str, Any]

FORECAST_ACCOUNTING_FORMAT = '$#,##0.00;[Red]($#,##0.00);$0.00'
_ENTITY_KINDS = {"customer", "vendor", "customer_entity", "vendor_entity", "entity"}
_SECTION_KINDS = {"section", "section_label", "cash_in_section", "cash_out_section"}
_TOTAL_KINDS = {
    "beginning_balance",
    "beginning_bank_balance",
    "total",
    "total_cash_in",
    "total_cash_out",
    "ending_balance",
    "ending_bank_balance",
}


def _mapping(value: object) -> Payload:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def _sequence(value: object) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _canonical_content(data: Payload) -> Payload:
    forecast = _mapping(data.get("forecast"))
    candidate: object = (
        forecast.get("export")
        or forecast.get("forecast_export")
        or data.get("forecast_export")
        or data.get("export")
    )
    envelope = _mapping(candidate)
    content = _mapping(envelope.get("content")) or envelope
    if content.get("weeks") is not None and content.get("rows") is not None:
        return content
    return _legacy_content(forecast)


def _legacy_content(forecast: Payload) -> Payload:
    """Project the pre-DTO compatibility shape into display rows without math."""
    weeks = [{"week_ending": value, "status": ""} for value in _sequence(forecast.get("week_headers"))]
    n = len(weeks)

    def amounts(key: str) -> list[Any]:
        values = _sequence(forecast.get(key))
        return values if values else [None] * n

    rows: list[Payload] = [{
        "row_kind": "beginning_bank_balance",
        "label": "Beginning Bank Balance",
        "amounts": amounts("beginning_balances"),
    }, {
        "row_kind": "section_label",
        "label": "Customer Collections (Cash In)",
        "amounts": [None] * n,
    }]
    for entity in _sequence(forecast.get("customers")):
        item = _mapping(entity)
        rows.append({
            "row_kind": "customer_entity",
            "label": item.get("name", ""),
            "effective_day": item.get("effective_day", item.get("day_of_month")),
            "effective_avg": item.get("effective_avg", item.get("avg_amount")),
            "amounts": _sequence(item.get("amounts")),
        })
    rows.extend(({
        "row_kind": "manual_cash_in",
        "label": "Other Cash In",
        "amounts": amounts("other_cash_in"),
    }, {
        "row_kind": "total_cash_in",
        "label": "Total Cash In",
        "amounts": amounts("total_cash_in"),
    }, {
        "row_kind": "section_label",
        "label": "Vendor Obligations (Cash Out)",
        "amounts": [None] * n,
    }))
    for entity in _sequence(forecast.get("vendors")):
        item = _mapping(entity)
        rows.append({
            "row_kind": "vendor_entity",
            "label": item.get("name", ""),
            "effective_day": item.get("effective_day", item.get("day_of_month")),
            "effective_avg": item.get("effective_avg", item.get("avg_amount")),
            "amounts": _sequence(item.get("amounts")),
        })
    rows.extend(({
        "row_kind": "manual_cash_out",
        "label": "Non Routine Expenses",
        "amounts": amounts("other_cash_out"),
    }, {
        "row_kind": "total_cash_out",
        "label": "Total Cash Out",
        "amounts": amounts("total_cash_out"),
    }, {
        "row_kind": "ending_bank_balance",
        "label": "Ending Bank Balance",
        "amounts": amounts("ending_balances"),
    }))
    return {"weeks": weeks, "rows": rows}


def _week_header(week: Payload) -> str:
    ending = str(week.get("week_ending") or "")
    status = str(week.get("status") or week.get("status_label") or "").strip()
    return f"{ending}\n{status}" if status else ending


def _write_money(ctx: ExcelContext, ws: Any, row: int, col: int, value: object) -> None:
    cell = ws.cell(row=row, column=col)
    cell.value = None if value is None else float(value)  # display edge; DTO equality stays Decimal
    cell.number_format = FORECAST_ACCOUNTING_FORMAT
    cell.alignment = ctx.Alignment(horizontal="right")
    cell.border = ctx.thin_border


def render_forecast_grid(ctx: ExcelContext, data: Payload) -> None:
    """Render Entity, Day, Avg/Mo, then every canonical week/status and row."""
    content = _canonical_content(data)
    weeks = [_mapping(value) for value in _sequence(content.get("weeks"))]
    rows = [_mapping(value) for value in _sequence(content.get("rows"))]

    ws = ctx.wb.create_sheet("13-Week Forecast")
    ws.sheet_properties.tabColor = ctx.ACCENT
    ctx.safe_text(ws, 1, 1, ctx.company).font = ctx.title_font
    ctx.safe_text(ws, 2, 1, f"13-Week Cash Flow Forecast — {ctx.period}").font = ctx.subtitle_font

    if not weeks or not rows:
        ctx.safe_text(ws, 4, 1, "Cash-flow forecast data is not available for this period.").font = ctx.Font(
            name="Calibri", italic=True, color=ctx.MUTED)
        ctx.freeze_and_size(ws, "D5", {1: 34, 2: 9, 3: 15})
        return

    ctx.set_header_row(ws, 4, ["Entity", "Day", "Avg/Mo", *(_week_header(week) for week in weeks)])
    ws.row_dimensions[4].height = 31

    for row_number, dto_row in enumerate(rows, start=5):
        kind = str(dto_row.get("row_kind") or "")
        label = dto_row.get("label", "")
        label_cell = ctx.safe_text(ws, row_number, 1, label)
        label_cell.border = ctx.thin_border

        if kind in _ENTITY_KINDS:
            day = dto_row.get("effective_day")
            day_cell = ws.cell(row=row_number, column=2, value=day)
            day_cell.border = ctx.thin_border
            day_cell.alignment = ctx.Alignment(horizontal="center")
            _write_money(ctx, ws, row_number, 3, dto_row.get("effective_avg"))
        else:
            for column in (2, 3):
                blank = ws.cell(row=row_number, column=column, value=None)
                blank.border = ctx.thin_border

        values = _sequence(dto_row.get("amounts"))
        for index in range(len(weeks)):
            _write_money(
                ctx,
                ws,
                row_number,
                4 + index,
                values[index] if index < len(values) else None,
            )

        if kind in _SECTION_KINDS:
            label_cell.font = ctx.section_font
            for column in range(1, len(weeks) + 4):
                ws.cell(row=row_number, column=column).fill = ctx.alt_fill
        elif kind in _TOTAL_KINDS:
            for column in range(1, len(weeks) + 4):
                cell = ws.cell(row=row_number, column=column)
                cell.font = ctx.total_font
                cell.fill = ctx.total_fill

    widths = {1: 34, 2: 9, 3: 15}
    widths.update({column: 15 for column in range(4, len(weeks) + 4)})
    ctx.freeze_and_size(ws, "D5", widths)
    ws.auto_filter.ref = f"A4:{ws.cell(row=4, column=len(weeks) + 3).coordinate}"


def render_runway_summary(ctx: ExcelContext, data: Payload) -> None:
    """Preserve the standalone runway section from the captured DTO summary."""
    forecast = _mapping(data.get("forecast"))
    content = _canonical_content(data)
    runway = _mapping(data.get("runway"))
    if not runway:
        runway = _mapping(content.get("runway"))

    ws = ctx.wb.create_sheet("Runway Summary")
    ws.sheet_properties.tabColor = ctx.NAVY
    ctx.safe_text(ws, 1, 1, ctx.company).font = ctx.title_font
    ctx.safe_text(ws, 2, 1, f"Cash Runway — {ctx.period}").font = ctx.subtitle_font
    ctx.set_header_row(ws, 4, ["Metric", "Value"])

    opening = (
        runway.get("opening_cash")
        if "opening_cash" in runway
        else forecast.get("opening_balance", content.get("opening_cash"))
    )
    opening_as_of = forecast.get("opening_balance_as_of", content.get("bank_balance_as_of"))
    lines = [
        ("Opening cash balance", opening, True),
        ("Opening balance as of", opening_as_of, False),
        ("Lowest forecast balance", runway.get("lowest_balance"), True),
        ("Lowest-balance week (forecast wk #)", runway.get("lowest_balance_week"), False),
        ("Lowest-balance date", runway.get("lowest_balance_date"), False),
        (
            "Weeks to negative cash",
            "Not within horizon" if runway.get("weeks_to_negative") is None else runway.get("weeks_to_negative"),
            False,
        ),
    ]
    for row_number, (label, value, is_money) in enumerate(lines, start=5):
        ctx.safe_text(ws, row_number, 1, label).border = ctx.thin_border
        if is_money:
            _write_money(ctx, ws, row_number, 2, value)
        else:
            ctx.safe_text(ws, row_number, 2, "—" if value is None else value).border = ctx.thin_border

    ctx.freeze_and_size(ws, "A5", {1: 36, 2: 24})
