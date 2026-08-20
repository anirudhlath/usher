import type { ReactElement } from 'react'
import { describe, expect, it } from 'vitest'
import { HttpResponse, http } from 'msw'
import { renderApp, screen, waitFor } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { server } from '@/test/server'
import {
  problemHandler,
  regenerateQueued,
  rowProviderUnknown,
  rowProviders,
  sourceUnavailable,
} from '@/test/fixtures'
import { ToastProvider } from '@/patterns'
import { ToastStack } from '@/features/shared/ToastStack'
import { TRACE_ID } from '@/test/fixtures/ids'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { ROUTES } from '@/app/routes'
import type { RowProviderResponse } from '@/api'
import Rows from './Rows'

/**
 * The ten slugs the backend's registry actually constructs — `slug_prefix` off
 * each `RowProvider`, verified by instantiating them.
 *
 * Same ten as the shared `rowProviders` fixture; this copy exists only to pin
 * its own enabled/disabled split, which several cases below count. When the two
 * disagree about *membership* that is a bug in one of them, so the assertion
 * under `describe('the registry')` compares the sets.
 */
const registry: RowProviderResponse[] = [
  { slug: 'continue-watching', enabled: true },
  { slug: 'next-up', enabled: true },
  { slug: 'recently-added', enabled: true },
  { slug: 'rediscover', enabled: true },
  { slug: 'because-you-watched', enabled: true },
  { slug: 'franchise', enabled: true },
  { slug: 'genre-affinity', enabled: true },
  { slug: 'seasonal', enabled: true },
  { slug: 'people', enabled: true },
  { slug: 'curated', enabled: false },
]

function withRegistry(providers: RowProviderResponse[] = registry) {
  return http.get('/admin/rows/providers', () => HttpResponse.json(providers))
}

function renderRows(ui: ReactElement = <Rows />) {
  return renderApp(
    <ToastProvider>
      {ui}
      <ToastStack />
    </ToastProvider>,
    { theme: 'light', density: 'compact', route: ROUTES.rows },
  )
}

describe('Rows', () => {
  describe('the registry', () => {
    it('names the same ten providers as the shared fixture', () => {
      // Two copies of a vocabulary is two chances for one to go stale, and the
      // one that goes stale silently is the fixture — a screen test passing
      // against slugs no provider registers is a test agreeing with itself.
      // This case is what makes the duplication safe: the split differs on
      // purpose, the membership may not.
      expect(registry.map((one) => one.slug).sort()).toEqual(rowProviders.map((one) => one.slug).sort())
    })
  })

  describe('ready', () => {
    it('explains every registered slug in plain language, bound to its switch', async () => {
      server.use(withRegistry())
      renderRows()

      await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(10))

      expect(screen.getByRole('switch', { name: 'continue-watching' })).toHaveAccessibleDescription(
        'Titles you are part-way through. Always pinned first.',
      )
      expect(screen.getByRole('switch', { name: 'rediscover' })).toHaveAccessibleDescription(
        'Played, and last played more than two years ago.',
      )
      expect(screen.getByRole('switch', { name: 'curated' })).toHaveAccessibleDescription(
        /52 of 59 generated headings were the plain genre labels the prompt forbade/,
      )
    })

    it('says what a prefix slug means, on the control and once on the screen', async () => {
      server.use(withRegistry())
      renderRows()

      await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(10))

      // A family: one switch, many rows.
      expect(screen.getByRole('switch', { name: 'franchise' })).toHaveAccessibleDescription(
        /The slug is a prefix — one switch governs every row whose own slug starts with franchise-\./,
      )
      // A literal: it can only ever build the one row.
      expect(screen.getByRole('switch', { name: 'next-up' }).getAttribute('aria-describedby')).not.toBeNull()
      expect(screen.getByRole('switch', { name: 'next-up' })).toHaveAccessibleDescription(
        'The next unplayed episode of a series you are mid-way through. Owned copies only.',
      )
      expect(screen.getByText('franchise-<collection>')).toBeInTheDocument()
    })

    it('refuses to invent a sentence for a slug it has never been told about', async () => {
      // Served explicitly rather than leaned on: this used to rely on the
      // shared fixture carrying five slugs no provider registers, so the case
      // passed because the *fixture* was wrong. A test whose premise is a
      // defect elsewhere stops working the moment that defect is fixed, and
      // says nothing in the meantime about the behaviour it names.
      server.use(withRegistry([...rowProviders, rowProviderUnknown]))
      renderRows()

      await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(11))
      expect(screen.getByRole('switch', { name: rowProviderUnknown.slug })).toHaveAccessibleDescription(
        new RegExp(`carries no plain-language description for ${rowProviderUnknown.slug}`),
      )
    })

    it('counts what is enabled without inventing a denominator', async () => {
      server.use(withRegistry())
      renderRows()
      await waitFor(() => expect(screen.getByText('9 of 10 enabled')).toBeInTheDocument())
    })
  })

  describe('never built versus disabled', () => {
    it('draws them as two different facts', async () => {
      server.use(withRegistry())
      const { container } = renderRows()

      await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(10))

      // Disabled: the switch is off and the row says what that means.
      const curated = screen.getByRole('switch', { name: 'curated' })
      expect(curated).toHaveAttribute('aria-checked', 'false')
      expect(screen.getByText('off — no row proposed')).toBeInTheDocument()

      // Never built: the switch is ON, and the row is inactive rather than off.
      // `/home` composes continue-watching, because-you-watched-* and
      // recently-added, so the other six enabled providers proposed nothing.
      const franchise = screen.getByRole('switch', { name: 'franchise' })
      expect(franchise).toHaveAttribute('aria-checked', 'true')
      expect(screen.getAllByText('inactive').length).toBeGreaterThan(0)
      expect(
        screen.getAllByText('on, and it proposed no row in the current composition').length,
      ).toBeGreaterThan(0)

      // And the fact that "has it EVER built one" is unanswerable gets §2's
      // never-computed treatment, with the missing fields named.
      expect(screen.getByText('Build history has never been on the wire')).toBeInTheDocument()
      expect(
        screen.getByText('GET /admin/rows/providers → {slug, enabled} · no built_at, no last_built'),
      ).toBeInTheDocument()
      expect(container.querySelector('.u-state--never')).not.toBeNull()

      // A provider that did compose is neither of the two.
      expect(screen.getAllByText('in the current home composition')).toHaveLength(3)
    })
  })

  describe('regeneration', () => {
    it('names the deployment-wide consequence before the click, and says what is unmeasured', async () => {
      server.use(withRegistry())
      const { user } = renderRows()
      await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(10))

      await user.click(screen.getByRole('button', { name: 'Regenerate rows' }))

      const dialog = screen.getByRole('dialog')
      expect(dialog).toHaveAttribute('aria-modal', 'true')
      expect(screen.getByText('Regenerate every home row?')).toBeInTheDocument()
      expect(screen.getByText('the whole deployment, not one person')).toBeInTheDocument()
      expect(screen.getByText('the composed home cache (30 s ETag)')).toBeInTheDocument()
      expect(screen.getByText('up to 10 providers, sequentially')).toBeInTheDocument()
      // patterns.md §5: no invented duration.
      expect(screen.getByText('not measured on this deployment')).toBeInTheDocument()
    })

    it('raises a receipt that says Queued and prints the key', async () => {
      server.use(withRegistry())
      const { user } = renderRows()
      await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(10))

      await user.click(screen.getByRole('button', { name: 'Regenerate rows' }))
      await user.click(screen.getByRole('button', { name: 'Queue regeneration' }))

      const toast = await screen.findByText('Queued a row regeneration')
      expect(toast).toBeInTheDocument()
      expect(screen.getByText(`key ${regenerateQueued.key}`)).toBeInTheDocument()
      expect(screen.getByRole('link', { name: 'Watch it on Pipeline' })).toHaveAttribute(
        'href',
        `/console${ROUTES.pipeline}`,
      )
      // Never "Done", never "Saved".
      expect(screen.queryByText(/^Done/)).not.toBeInTheDocument()
      expect(screen.queryByText(/^Saved/)).not.toBeInTheDocument()
    })

    it('toggling a provider is a notice, not a receipt — the PUT answers 200', async () => {
      server.use(withRegistry())
      const { user } = renderRows()
      await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(10))

      await user.click(screen.getByRole('switch', { name: 'curated' }))

      expect(await screen.findByText('Enabled curated')).toBeInTheDocument()
      expect(screen.queryByText(/^key /)).not.toBeInTheDocument()
    })
  })

  describe('loading', () => {
    it('shows a table-shaped skeleton, not a route spinner', () => {
      renderRows()
      expect(screen.getByText('Loading the row providers …')).toBeInTheDocument()
    })
  })

  describe('empty', () => {
    it('says an empty registry is an empty registry, not everything switched off', async () => {
      server.use(http.get('/admin/rows/providers', () => HttpResponse.json([])))
      renderRows()

      expect(await screen.findByText('Computed, and empty')).toBeInTheDocument()
      expect(screen.getByText('GET /admin/rows/providers → []')).toBeInTheDocument()
      expect(screen.queryByRole('switch')).not.toBeInTheDocument()
    })
  })

  describe('the trace link (patterns.md §3)', () => {
    function renderFailed(tempoUrl: string | null, traceId?: string) {
      server.use(
        problemHandler(
          'get',
          '/admin/rows/providers',
          sourceUnavailable('/admin/rows/providers'),
          traceId === undefined ? {} : { traceId },
        ),
      )
      return renderApp(
        withTempo(
          <ToastProvider>
            <Rows />
            <ToastStack />
          </ToastProvider>,
          tempoUrl,
        ),
        { theme: 'light', density: 'compact', route: ROUTES.rows },
      )
    }

    it('opens the trace in Tempo when the response carried one', async () => {
      const { container } = renderFailed(TEMPO_URL, TRACE_ID)
      await screen.findByText('code source_unavailable')

      expect(traceLinks(container)[0]?.getAttribute('href')).toContain(TRACE_ID)
    })

    it('emits no anchor at all when Tempo is unconfigured', async () => {
      const { container } = renderFailed(null, TRACE_ID)
      await screen.findByText('code source_unavailable')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })

    it('emits no anchor when the response carried no traceresponse header', async () => {
      const { container } = renderFailed(TEMPO_URL)
      await screen.findByText('code source_unavailable')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })
  })

  describe('error', () => {
    it('shows code, status and the server detail verbatim, and offers the retry', async () => {
      server.use(
        problemHandler('get', '/admin/rows/providers', sourceUnavailable('/admin/rows/providers'), {
          retryAfter: 30,
        }),
      )
      renderRows()

      expect(await screen.findByText('Living Room Emby did not answer within 5.0 s.')).toBeInTheDocument()
      expect(screen.getByText('code source_unavailable')).toBeInTheDocument()
      expect(screen.getByText('HTTP 503')).toBeInTheDocument()
      expect(screen.getByText('retry after 30s')).toBeInTheDocument()
      expect(screen.queryByRole('switch')).not.toBeInTheDocument()
    })
  })

  describe('accessibility', () => {
    it('has no axe violations', async () => {
      server.use(withRegistry())
      const { container } = renderRows()
      await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(10))
      await expectNoViolations(container)
    })

    it('has no axe violations with the confirm dialog open', async () => {
      server.use(withRegistry())
      const { container, user } = renderRows()
      await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(10))
      await user.click(screen.getByRole('button', { name: 'Regenerate rows' }))
      await expectNoViolations(container)
    })
  })
})
