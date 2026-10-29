"""PDF sections for the canonical cash-flow forecast export grid."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from .pdf_context import PdfContext, _xml

Payload = dict[str, Any]
Story = list[object]

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


def _legacy_content(forecast: Payload) -> Payload:
    weeks = [{"week_ending": value, "status": ""} for value in _sequence(forecast.get("week_headers"))]
    n = len(weeks)

    def amounts(key: str) -> list[Any]:
        values = _sequence(forecast.get(key))
        return values if values else [None] * n

    rows: list[Payload] = [{
        "row_kind": "beginning_bank_balance", "label": "Beginning Bank Balance",
        "amounts": amounts("beginning_balances"),
    }, {
        "row_kind": "section_label", "label": "Customer Collections (Cash In)",
        "amounts": [None] * n,
    }]
    for entity in _sequence(forecast.get("customers")):
        item = _mapping(entity)
        rows.append({
            "row_kind": "customer_entity", "label": item.get("name", ""),
            "effective_day": item.get("effective_day", item.get("day_of_month")),
            "effective_avg": item.get("effective_avg", item.get("avg_amount")),
            "amounts": _sequence(item.get("amounts")),
        })
    rows.extend((
        {"row_kind": "manual_cash_in", "label": "Other Cash In", "amounts": amounts("other_cash_in")},
        {"row_kind": "total_cash_in", "label": "Total Cash In", "amounts": amounts("total_cash_in")},
        {"row_kind": "section_label", "label": "Vendor Obligations (Cash Out)", "amounts": [None] * n},
    ))
    for entity in _sequence(forecast.get("vendors")):
        item = _mapping(entity)
        rows.append({
            "row_kind": "vendor_entity", "label": item.get("name", ""),
            "effective_day": item.get("effective_day", item.get("day_of_month")),
            "effective_avg": item.get("effective_avg", item.get("avg_amount")),
            "amounts": _sequence(item.get("amounts")),
        })
    rows.extend((
        {"row_kind": "manual_cash_out", "label": "Non Routine Expenses", "amounts": amounts("other_cash_out")},
        {"row_kind": "total_cash_out", "label": "Total Cash Out", "amounts": amounts("total_cash_out")},
        {"row_kind": "ending_bank_balance", "label": "Ending Bank Balance", "amounts": amounts("ending_balances")},
    ))
    return {"weeks": weeks, "rows": rows}


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


def _money(value: object) -> str:
    if value is None:
        return "—"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "—"
    if amount == 0:
        amount = Decimal("0.00")
    if amount < 0:
        return f"(${abs(amount):,.2f})"
    return f"${amount:,.2f}"


def _week_label(week: Payload) -> str:
    ending = str(week.get("week_ending") or "")
    status = str(week.get("status") or week.get("status_label") or "").strip()
    short_ending = ending[5:] if len(ending) >= 10 else ending
    return (
        f"{_xml(short_ending)}<br/>{_xml(status)}"
        if status
        else _xml(short_ending)
    )


def render_forecast_grid(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Render every canonical DTO row without recomputing a financial cell."""
    from reportlab.platypus import HRFlowable, Paragraph, Spacer, Table, TableStyle

    content = _canonical_content(data)
    weeks = [_mapping(value) for value in _sequence(content.get("weeks"))]
    rows = [_mapping(value) for value in _sequence(content.get("rows"))]

    story.append(Paragraph(_xml(ctx.company), ctx.S_LAND_HDR))
    story.append(Paragraph(
        '<font size="10"><b>13-Week Cash Flow Forecast</b></font><br/>'
        f'<font size="8" color="#6b7280">As of {_xml(ctx.period)}</font>',
        ctx.style("FcLandSub", fontSize=10, spaceBefore=0, spaceAfter=6),
    ))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ctx.NAVY))
    story.append(Spacer(1, 4))
    if not weeks or not rows:
        story.append(Paragraph("Cash-flow forecast data is not available for this period.", ctx.S_BODY))
        return

    header_style = ctx.style(
        "CffHeader", fontName="Helvetica-Bold", fontSize=4.0, leading=8.0,
        textColor=ctx.WHITE, alignment=1,
    )
    label_style = ctx.style("CffLabel", fontSize=4.5, leading=6.8, wordWrap="LTR")
    cell_style = ctx.style("CffCell", fontSize=4.2, leading=6.0, alignment=2)
    headers: list[object] = [
        Paragraph("Entity", header_style), Paragraph("Day", header_style),
        Paragraph("Avg/Mo", header_style),
        *(Paragraph(_week_label(week), header_style) for week in weeks),
    ]
    grid: list[list[object]] = [headers]
    section_rows: list[int] = []
    total_rows: list[int] = []

    for dto_row in rows:
        kind = str(dto_row.get("row_kind") or "")
        values = _sequence(dto_row.get("amounts"))
        if kind in _ENTITY_KINDS:
            day = "" if dto_row.get("effective_day") is None else str(dto_row.get("effective_day"))
            avg = _money(dto_row.get("effective_avg"))
        else:
            day, avg = "", ""
        rendered = [
            Paragraph(_xml(dto_row.get("label", "")), label_style),
            Paragraph(day, cell_style),
            Paragraph(avg, cell_style),
        ]
        rendered.extend(
            Paragraph(_money(values[index] if index < len(values) else None), cell_style)
            for index in range(len(weeks))
        )
        grid.append(rendered)
        row_index = len(grid) - 1
        if kind in _SECTION_KINDS:
            section_rows.append(row_index)
        elif kind in _TOTAL_KINDS:
            total_rows.append(row_index)

    entity_width = 1.42 * ctx.inch
    day_width = 0.34 * ctx.inch
    avg_width = 0.59 * ctx.inch
    week_width = (ctx.LAND_USABLE_W - entity_width - day_width - avg_width) / len(weeks)
    table = Table(
        grid,
        colWidths=[entity_width, day_width, avg_width, *([week_width] * len(weeks))],
        repeatRows=1,
        splitByRow=1,
    )
    commands: list[tuple[Any, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), ctx.NAVY_LIGHT),
        ("GRID", (0, 0), (-1, -1), 0.2, ctx.GRID_LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2),
        ("LEFTPADDING", (0, 0), (-1, -1), 1.5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1.5),
    ]
    for index in section_rows:
        commands.extend((
            ("BACKGROUND", (0, index), (-1, index), ctx.SILVER),
            ("FONTNAME", (0, index), (-1, index), "Helvetica-Bold"),
        ))
    for index in total_rows:
        commands.extend((
            ("BACKGROUND", (0, index), (-1, index), ctx.TOTAL_BG),
            ("FONTNAME", (0, index), (-1, index), "Helvetica-Bold"),
            ("LINEABOVE", (0, index), (-1, index), 0.5, ctx.SLATE),
        ))
    table.setStyle(TableStyle(commands))
    story.append(table)


def render_runway_summary(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Preserve the captured runway summary alongside the exact grid."""
    from reportlab.platypus import HRFlowable, Paragraph, Spacer

    forecast = _mapping(data.get("forecast"))
    content = _canonical_content(data)
    runway = _mapping(data.get("runway")) or _mapping(content.get("runway"))
    opening = (
        runway.get("opening_cash")
        if "opening_cash" in runway
        else forecast.get("opening_balance", content.get("opening_cash"))
    )
    opening_as_of = forecast.get("opening_balance_as_of", content.get("bank_balance_as_of"))

    story.append(Paragraph(f"Cash Runway — {_xml(ctx.period)}", ctx.S_SECTION_HDR))
    story.append(HRFlowable(width="100%", thickness=2, color=ctx.NAVY))
    story.append(Spacer(1, 6))
    weeks_to_negative = runway.get("weeks_to_negative")
    rows: list[list[object]] = [
        ["Opening cash balance", _money(opening)],
        ["Opening balance as of", "—" if opening_as_of is None else str(opening_as_of)],
        ["Lowest forecast balance", _money(runway.get("lowest_balance"))],
        ["Lowest-balance week (forecast wk #)", "—" if runway.get("lowest_balance_week") is None else str(runway.get("lowest_balance_week"))],
        ["Lowest-balance date", "—" if runway.get("lowest_balance_date") is None else str(runway.get("lowest_balance_date"))],
        ["Weeks to negative cash", "Not within horizon" if weeks_to_negative is None else str(weeks_to_negative)],
    ]
    story.append(ctx.table(
        ["Metric", "Value"], rows,
        col_widths=[ctx.USABLE_W * 0.55, ctx.USABLE_W * 0.45],
    ))
