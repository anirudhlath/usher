import type { ReactElement, ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { render, type RenderOptions, type RenderResult } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CONSOLE_BASE } from '@/api/paths'

export interface UsherRenderOptions extends Omit<RenderOptions, 'wrapper'> {
  /** Initial route, written *without* the console's basename. */
  route?: string
  /** `light` + `compact` is the operator default; viewer surfaces are pinned dark. */
  theme?: 'dark' | 'light'
  density?: 'comfortable' | 'compact'
  queryClient?: QueryClient
}

/**
 * `retry: false` matters more than it looks: with the app's real retry policy a
 * test asserting an error state waits for the backoff before the state exists,
 * and reads as a flake rather than as a slow test.
 */
export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  })
}

export interface UsherRenderResult extends RenderResult {
  queryClient: QueryClient
  user: ReturnType<typeof userEvent.setup>
}

export function renderApp(ui: ReactElement, options: UsherRenderOptions = {}): UsherRenderResult {
  const {
    route = '/',
    theme = 'dark',
    density = 'comfortable',
    queryClient = createTestQueryClient(),
    ...rest
  } = options

  document.documentElement.setAttribute('data-theme', theme)
  if (density === 'compact') document.documentElement.setAttribute('data-density', 'compact')
  else document.documentElement.removeAttribute('data-density')

  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter basename={CONSOLE_BASE} initialEntries={[`${CONSOLE_BASE}${route}`]}>
          {children}
        </MemoryRouter>
      </QueryClientProvider>
    )
  }

  return {
    ...render(ui, { wrapper: Wrapper, ...rest }),
    queryClient,
    user: userEvent.setup(),
  }
}

/** For design-system components, which know nothing about routing or the API. */
export function renderComponent(
  ui: ReactElement,
  options: Omit<UsherRenderOptions, 'route'> = {},
): UsherRenderResult {
  const { theme = 'dark', density = 'comfortable', ...rest } = options
  document.documentElement.setAttribute('data-theme', theme)
  if (density === 'compact') document.documentElement.setAttribute('data-density', 'compact')
  else document.documentElement.removeAttribute('data-density')
  return {
    ...render(ui, rest),
    queryClient: createTestQueryClient(),
    user: userEvent.setup(),
  }
}

export * from '@testing-library/react'
export { userEvent }
