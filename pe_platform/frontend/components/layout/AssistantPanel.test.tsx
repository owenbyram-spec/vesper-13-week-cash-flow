import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AssistantStreamEvent, AssistantMessageMetadata } from '@/lib/api/assistant';

vi.mock('next/navigation', () => ({ usePathname: () => '/assistant' }));
vi.mock('@/lib/store', () => ({ useAppStore: () => ({ activeCompanyId: 'synthetic-company' }) }));
vi.mock('@/lib/workspace-scope', () => ({
    currentWorkspaceScope: () => ({ id: 'fixture-workspace', revision: 1, realmId: 'synthetic-company' }),
    isCurrentScope: () => true,
    registerWorkspaceStream: () => () => {},
    StaleWorkspaceError: class extends Error {},
}));
vi.mock('@/lib/api/client', () => ({
    apiClient: { get: vi.fn(), post: vi.fn() },
    BackendConfigurationError: class extends Error {},
    getApiBaseUrl: () => '/bff',
    buildApiHeaders: () => ({}),
}));
vi.mock('@/lib/api/assistant', async (original) => ({
    ...await original<typeof import('@/lib/api/assistant')>(), streamAssistantMessage: vi.fn(),
}));
import { apiClient } from '@/lib/api/client';
import { streamAssistantMessage } from '@/lib/api/assistant';
import { AssistantPanel } from './AssistantPanel';

const metadata = (status: NonNullable<AssistantMessageMetadata['verification']>['status']): AssistantMessageMetadata => ({
    verification_schema_version: 1,
    verification: { status, scope: 'numeric_claims', provisional: false },
    generated_at: '2026-09-27T12:00:00Z', data_freshness_at: null,
    critical_warnings: ['first warning', 'second warning', 'third warning', 'fourth warning', 'semantic mismatch last'],
    caveats: ['first caveat', 'second caveat', 'third caveat'],
    observation: { status: 'incomplete', reasons: ['Usage recording failed.'] },
});

beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(apiClient.get).mockResolvedValue({ data: [] });
    Element.prototype.scrollIntoView = vi.fn();
});

async function send() {
    await act(async () => { render(<AssistantPanel />); });
    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'Explain the result' } });
    await act(async () => { fireEvent.submit(input.closest('form')!); });
}

describe('assistant verification presentation', () => {
    it.each([
        ['conflict', 'Numeric conflict — review required'],
        ['limited', 'Limited numeric checks'],
        ['no_numeric_claims', 'No numeric claims checked'],
        ['not_checked', 'Not checked'],
        ['checked_numeric_claims', 'Numeric claims checked'],
    ] as const)('shows %s and every warning without fabricated freshness', async (status, label) => {
        vi.mocked(apiClient.get).mockImplementation(async (url) => ({ data: String(url).includes('/history')
            ? [{ role: 'assistant', content: 'Saved answer', metadata: metadata(status) }] : [] }));
        render(<AssistantPanel />);
        expect(await screen.findByText(label)).toBeInTheDocument();
        expect(screen.getByText('semantic mismatch last')).toBeInTheDocument();
        expect(screen.getByText('third caveat')).toBeInTheDocument();
        expect(screen.getByText(/Observation incomplete/)).toHaveTextContent('Usage recording failed.');
        expect(screen.getByText('Source freshness: Unknown')).toBeInTheDocument();
        expect(screen.queryByText(/% confidence/)).not.toBeInTheDocument();
    });

    it('treats old confidence and captured timestamps as unknown', async () => {
        vi.mocked(apiClient.get).mockImplementation(async (url) => ({ data: String(url).includes('/history')
            ? [{ role: 'assistant', content: 'Legacy', metadata: { confidence: .99, data_freshness_at: '2026-09-27T12:00:00Z' } }] : [] }));
        render(<AssistantPanel />);
        expect(await screen.findByText('Legacy response — verification unknown')).toBeInTheDocument();
        expect(screen.getByText('Source freshness: Unknown')).toBeInTheDocument();
        expect(screen.queryByText('99% confidence')).not.toBeInTheDocument();
    });

    it('shows completed numeric checks only after the final event', async () => {
        vi.mocked(streamAssistantMessage).mockImplementation(async (_payload, emit) => {
            emit({ type: 'text', content: 'Completed answer' });
            emit({ type: 'explanation', content: metadata('checked_numeric_claims') });
            emit({ type: 'done' });
        });
        await send();
        expect(await screen.findByText('Numeric claims checked')).toBeInTheDocument();
        expect(screen.queryByText('Provisional — final checks pending')).not.toBeInTheDocument();
    });

    it('keeps text provisional until completion and never certifies a failed stream', async () => {
        let emit!: (event: AssistantStreamEvent) => void;
        let fail!: (reason: Error) => void;
        vi.mocked(streamAssistantMessage).mockImplementation((_payload, handler) => {
            emit = handler;
            return new Promise((_resolve, reject) => { fail = reject; });
        });
        await send();
        await waitFor(() => expect(streamAssistantMessage).toHaveBeenCalled());
        act(() => { emit({ type: 'text', content: 'Partial answer' }); emit({ type: 'explanation', content: metadata('checked_numeric_claims') }); });
        expect(screen.getByText('Provisional — final checks pending')).toBeInTheDocument();
        expect(screen.queryByText('Numeric claims checked')).not.toBeInTheDocument();
        await act(async () => fail(new Error('disconnected')));
        expect(screen.getByText('Verification incomplete')).toBeInTheDocument();
        expect(screen.queryByText('Numeric claims checked')).not.toBeInTheDocument();
        expect(screen.getByText(/Partial answer/)).toBeInTheDocument();
    });
});

describe('real stream completion boundary', () => {
    it('does not replay a partial or incomplete stream as a query', async () => {
        const actual = await vi.importActual<typeof import('@/lib/api/assistant')>('@/lib/api/assistant');
        const body = new ReadableStream({ start(controller) {
            controller.enqueue(new TextEncoder().encode('data: {"type":"text","content":"Partial"}\n\n'));
            controller.close();
        } });
        const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(body, { status: 200 }));
        const events: AssistantStreamEvent[] = [];
        try {
            await expect(actual.streamAssistantMessage({ message: 'explain', realm_id: 'synthetic-company' }, (event) => events.push(event)))
                .rejects.toThrow('before completion');
            expect(events).toEqual([{ type: 'text', content: 'Partial' }]);
            expect(apiClient.post).not.toHaveBeenCalled();
        } finally { fetchMock.mockRestore(); }
    });
});
