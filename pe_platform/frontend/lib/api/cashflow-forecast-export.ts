import { apiClient } from "./client";

export type CashflowForecastExportFormat = "excel" | "pdf";

interface DownloadCashflowForecastExportOptions {
    exportId: string;
    expectedCompanyId: string;
    format: CashflowForecastExportFormat;
    /**
     * Re-checks page ownership after the blob response arrives and before any
     * browser-owned object (Blob, object URL, or anchor) is created. This keeps
     * a late prior-company response inert after a company switch.
     */
    isCurrentOwner: () => boolean;
}

const MIME_BY_FORMAT: Record<CashflowForecastExportFormat, string> = {
    excel: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    pdf: "application/pdf",
};

const EXTENSION_BY_FORMAT: Record<CashflowForecastExportFormat, string> = {
    excel: "xlsx",
    pdf: "pdf",
};

function responseFilename(
    contentDisposition: string | undefined,
    exportId: string,
    format: CashflowForecastExportFormat,
) {
    const encoded = contentDisposition?.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    if (encoded) {
        try {
            return decodeURIComponent(encoded.replace(/^"|"$/g, ""));
        } catch {
            // Fall through to the ordinary filename token or deterministic fallback.
        }
    }
    const ordinary = contentDisposition?.match(/filename="?([^";]+)"?/i)?.[1];
    return ordinary || `cashflow_forecast_${exportId}.${EXTENSION_BY_FORMAT[format]}`;
}

/**
 * Downloads one immutable CFF export by its visible-state identity.
 *
 * `expected_company_id` is deliberately required and is sent independently
 * from the opaque export ID. The endpoint, not ambient store state or a report
 * period, authorizes and renders the stored snapshot.
 */
export async function downloadCashflowForecastExport({
    exportId,
    expectedCompanyId,
    format,
    isCurrentOwner,
}: DownloadCashflowForecastExportOptions): Promise<"downloaded" | "discarded"> {
    const response = await apiClient.get(
        `/reporting/cashflow-forecast/exports/${encodeURIComponent(exportId)}/download`,
        {
            params: {
                expected_company_id: expectedCompanyId,
                format,
            },
            responseType: "blob",
        },
    );

    if (!isCurrentOwner()) return "discarded";

    const blob = new Blob([response.data], { type: MIME_BY_FORMAT[format] });
    const objectUrl = window.URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = responseFilename(
        response.headers?.["content-disposition"],
        exportId,
        format,
    );
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.URL.revokeObjectURL(objectUrl);
    return "downloaded";
}
