// PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-5 (F-4 #5 — CFF integrity).
// Cash-flow-forecast integrity API — categorized-but-unmapped entity detection + one-click
// row creation. NEW file (W-5 dispatch rule: new helpers go in a new file via the shared
// apiClient — no edits to shared lib files other wave agents may touch).
import { apiClient } from './client';

export interface UnmappedEntity {
    entity_name: string;
    entity_type: 'customer' | 'vendor';
    transaction_count: number;
    total_amount: number;
    first_week_ending: string | null;
    last_week_ending: string | null;
}

export interface UnmappedEntitiesResponse {
    company_id: string;
    since_weeks: number;
    count: number;
    unmapped: UnmappedEntity[];
}

export interface AddForecastRowResponse {
    status: string;
    entity_name: string;
    entity_type: string;
    override_id: number;
    snapshot_refresh_enqueued: boolean;
}

// NOTE: both fetchers intentionally THROW on failure (the lib/api/* contract) so callers
// can distinguish "genuinely no unmapped entities" from "the check failed to load".
export const getUnmappedEntities = async (companyId: string): Promise<UnmappedEntitiesResponse> => {
    const { data } = await apiClient.get<UnmappedEntitiesResponse>(
        '/cashflow-forecast/integrity/unmapped',
        { params: { company_id: companyId } },
    );
    return data;
};

export const addForecastRow = async (
    companyId: string,
    entityName: string,
    entityType: 'customer' | 'vendor',
): Promise<AddForecastRowResponse> => {
    const { data } = await apiClient.post<AddForecastRowResponse>(
        '/cashflow-forecast/integrity/add-row',
        { company_id: companyId, entity_name: entityName, entity_type: entityType },
    );
    return data;
};
