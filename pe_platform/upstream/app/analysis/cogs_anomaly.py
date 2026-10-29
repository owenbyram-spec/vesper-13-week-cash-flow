from ..qbo_client import QBOClient
from .profit_margin import profit_and_margin_by_month
import statistics

def cogs_anomalies(
    qbo_client: QBOClient, z_threshold: float = 2.0, period_floor: str | None = None
) -> dict:
    """
    Flags months where COGS is unusually high relative to the year-to-date average.

    ``period_floor`` (``YYYY-MM``, AC-A2 plan 20260716-five-domain-remediation):
    the SOFT-close floor — months at/below it are CLOSED and are never flagged
    (lexicographic ``YYYY-MM`` compare). The population statistics still use the
    full window: closed months remain valid baseline evidence; they just cannot
    become new alerts.
    """
    pm = profit_and_margin_by_month(qbo_client)
    months = pm["months"]
    cogs_values = [m["cogs"] for m in months if m["cogs"] is not None]

    if len(cogs_values) < 2:
        return {
            "months": months,
            "anomalies": [],
            "message": "Not enough COGS data points to compute anomalies.",
        }

    mean_cogs = statistics.mean(cogs_values)
    stdev_cogs = statistics.pstdev(cogs_values)

    anomalies = []
    for m in months:
        cogs = m["cogs"]
        if period_floor is not None and m["month"] <= period_floor:
            continue  # closed month (AC-A2): baseline evidence only, never a flag
        if stdev_cogs > 0:
            z = (cogs - mean_cogs) / stdev_cogs
        else:
            z = 0.0
        if z >= z_threshold:
            anomalies.append(
                {
                    "month": m["month"],
                    "cogs": cogs,
                    "z_score": z,
                }
            )

    return {
        "mean_cogs": mean_cogs,
        "stdev_cogs": stdev_cogs,
        "months": months,
        "anomalies": anomalies,
    }
