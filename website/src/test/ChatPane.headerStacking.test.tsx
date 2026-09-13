import { describe, it, expect, vi } from 'vitest'
import type { ReactNode } from 'react'
import { render } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* The pane title row is flex-flow chrome inside a pane whose root opens no
 * stacking context, so any z-index on it competes with the SHELL's layers,
 * not just the pane's own. At z-50 the split-pane headers painted over the
 * mobile workspace overlay (z-[47], ChatPage) and the sessions-drawer scrim
 * (z-[46]): the panel's tab strip disappeared under the pane titles and its
 * own controls were unreachable. The row only needs to clear the pane's z-[1]
 * / z-[2] message chrome. These pins keep it below every shell overlay. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'

const SLOT = 'chat-1-stacking'

/** Shell overlays the pane header must stay under: the sessions-drawer scrim
 *  (z-[46]) and the mobile workspace panel (z-[47]) in ChatPage. */
const SHELL_OVERLAY_FLOOR = 46

function mount() {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, title: 'Stacking pane', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: '2026-09-01T00:00:00Z' }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: SLOT, slotState: 'idle', messages: [] } as unknown as RootState['chat'],
    } as Partial<RootState>,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ChatPane slotKey={SLOT} />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('ChatPane title row stacking', () => {
  it('keeps the pane title row below every shell overlay', () => {
    const { container } = mount()
    const row = container.querySelector('.panel-toolbar') as HTMLElement
    expect(row).not.toBeNull()
    const z = row.className.match(/\bz-\[?(\d+)\]?/g) ?? []
    expect(z).toHaveLength(1)
    const value = Number(z[0].replace(/\D/g, ''))
    expect(value).toBeGreaterThan(2) // above the pane's z-[1] / z-[2] message chrome
    expect(value).toBeLessThan(SHELL_OVERLAY_FLOOR)
  })
})
