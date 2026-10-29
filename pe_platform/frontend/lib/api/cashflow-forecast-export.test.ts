import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./client", () => ({
    apiClient: { get: vi.fn() },
}));

import { apiClient } from "./client";
import { downloadCashflowForecastExport } from "./cashflow-forecast-export";

describe("downloadCashflowForecastExport", () => {
    const createObjectURL = vi.fn(() => "blob:cff-export");
    const revokeObjectURL = vi.fn();
    const click = vi.fn();

    beforeEach(() => {
        vi.clearAllMocks();
        window.URL.createObjectURL = createObjectURL;
        window.URL.revokeObjectURL = revokeObjectURL;
        const anchor = document.createElement("a");
        anchor.click = click;
        vi.spyOn(anchor, "remove");
        vi.spyOn(document, "createElement").mockReturnValue(anchor);
        vi.mocked(apiClient.get).mockResolvedValue({
            data: new Uint8Array([1, 2, 3]),
            headers: { "content-disposition": 'attachment; filename="Acme_CFF.xlsx"' },
        });
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it("requires the visible owner and exact export ID in the request", async () => {
        const result = await downloadCashflowForecastExport({
            exportId: "cff-id-123",
            expectedCompanyId: "realm-1",
            format: "excel",
            isCurrentOwner: () => true,
        });

        expect(apiClient.get).toHaveBeenCalledWith(
            "/reporting/cashflow-forecast/exports/cff-id-123/download",
            {
                params: { expected_company_id: "realm-1", format: "excel" },
                responseType: "blob",
            },
        );
        expect(result).toBe("downloaded");
        expect(click).toHaveBeenCalledOnce();
        expect(revokeObjectURL).toHaveBeenCalledWith("blob:cff-export");
    });

    it("discards a late prior-owner response before reading blob data", async () => {
        const readData = vi.fn(() => new Uint8Array([9]));
        vi.mocked(apiClient.get).mockResolvedValue(Object.defineProperty(
            { headers: {} },
            "data",
            { get: readData },
        ));

        const result = await downloadCashflowForecastExport({
            exportId: "tenant-a-id",
            expectedCompanyId: "realm-a",
            format: "pdf",
            isCurrentOwner: () => false,
        });

        expect(result).toBe("discarded");
        expect(readData).not.toHaveBeenCalled();
        expect(createObjectURL).not.toHaveBeenCalled();
        expect(click).not.toHaveBeenCalled();
    });
});
