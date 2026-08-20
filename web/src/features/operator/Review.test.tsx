import { describe, expect, it } from 'vitest'
import { HttpResponse, http } from 'msw'
import { renderApp, screen, waitFor, within } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { server } from '@/test/server'
import { problemHandler, sourceUnavailable, unmatchedEmpty, validationFailed } from '@/test/fixtures'
import { ToastProvider } from '@/patterns'
import { ToastStack } from '@/features/shared/ToastStack'
import { TRACE_ID } from '@/test/fixtures/ids'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { ROUTES } from '@/app/routes'
import Review from './Review'

/** The two ids the default handler puts on page one, in order. */
const FIRST = '7f3a91c4e8b2'
const SECOND = 'b18c62d9f047'

function renderReview() {
  return renderApp(
    <ToastProvider>
      <Review />
      <ToastStack />
    </ToastProvider>,
    { theme: 'light', density: 'compact', route: ROUTES.review },
  )
}

/** The file panel, which is the thing the triage keys actually move. */
function selectedExternalId(): string | null {
  const panel = screen.getByRole('region', { name: 'Unmatched file' })
  return panel.querySelector('.u-mono')?.textContent ?? null
}

describe('Review', () => {
  describe('ready', () => {
    it('explains external_id as the handle it is', async () => {
      renderReview()
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))

      expect(screen.getByText(/This is the handle the source uses/)).toHaveTextContent(
        /the media server’s own id for the file, not a catalog title id and not anything you can look up in Usher/,
      )
    })

    it('lists the fields the left panel still needs, in mono', async () => {
      const { container } = renderReview()
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))

      expect(screen.getByText('Requires backend work')).toBeInTheDocument()
      for (const field of ['filename', 'container', 'resolution', 'runtime_seconds', 'library_name']) {
        const found = screen.getByText(field)
        expect(found).toHaveClass('u-mono')
      }
      expect(container.querySelector('.u-backendwork__routes')?.textContent).toContain('GET /admin/unmatched')
      expect(
        screen.getByText(/the matcher already computes confidence scores it does not return/),
      ).toBeInTheDocument()
    })

    it('says the queue offered no candidates at all', async () => {
      renderReview()
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))

      expect(screen.getByText('No candidates were offered')).toBeInTheDocument()
      expect(screen.getByText('GET /admin/unmatched — no candidates, no scores')).toBeInTheDocument()
    })

    it('pages by keyset with a button and no total', async () => {
      renderReview()
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))

      expect(screen.getByRole('button', { name: 'Load more' })).toBeInTheDocument()
      expect(screen.getByText('2 loaded so far')).toBeInTheDocument()
      expect(screen.getByText(/there is no total/)).toBeInTheDocument()
    })

    it('says so in a sentence when the keyset walk ends', async () => {
      const { user } = renderReview()
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))

      await user.click(screen.getByRole('button', { name: 'Load more' }))

      expect(await screen.findByText('That is everything we have for this filter.')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument()
    })

    it('distinguishes an item with no recorded arrival from one with a date', async () => {
      const { user } = renderReview()
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))
      expect(screen.getByText('2026-08-15T02:41:18Z')).toBeInTheDocument()

      // The second item's `added_at` is null: a delta walk saw it without
      // seeing it arrive, and that is not the same as a date of zero.
      await user.keyboard('j')
      await waitFor(() => expect(selectedExternalId()).toBe(SECOND))
      // The em dash is `StateBlock kind="na"`'s own: an absent key is "not
      // applicable to this record", not a grey dash standing in for four facts.
      expect(
        screen.getByText(/No arrival was recorded\. A delta walk sees an item without seeing it arrive\./),
      ).toHaveTextContent('— No arrival was recorded.')
    })
  })

  describe('triage keys', () => {
    it('moves with j and k, and moves the focus with the cursor', async () => {
      const { user } = renderReview()
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))

      await user.keyboard('j')
      await waitFor(() => expect(selectedExternalId()).toBe(SECOND))
      expect(document.activeElement).toHaveAttribute('data-queue-index', '1')

      await user.keyboard('k')
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))
      expect(document.activeElement).toHaveAttribute('data-queue-index', '0')

      // Clamped at both ends: holding a key never wraps past the boundary.
      await user.keyboard('k')
      expect(selectedExternalId()).toBe(FIRST)
    })

    it('skips forward with s', async () => {
      const { user } = renderReview()
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))

      await user.keyboard('s')
      await waitFor(() => expect(selectedExternalId()).toBe(SECOND))
    })

    it('resolves the selected candidate with Enter', async () => {
      const { user } = renderReview()
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))

      await user.type(screen.getByRole('combobox'), 'stal')
      const options = await screen.findAllByRole('option', { name: /Stalker/ })
      const first = options[0]
      expect(first).toBeDefined()
      if (first) await user.click(first)

      // Focus back inside the queue, which is where triage happens.
      await user.click(screen.getByRole('button', { name: /7f3a91c4e8b2/ }))
      await user.keyboard('{Enter}')

      expect(await screen.findByText(`Resolved ${FIRST} → Stalker`)).toBeInTheDocument()
      // 200 with the row it wrote, not 202 — so no key, and not "Queued".
      expect(screen.queryByText(/^key /)).not.toBeInTheDocument()
      expect(screen.queryByText(/Queued/)).not.toBeInTheDocument()
    })

    it('is inert while a text field has focus', async () => {
      const { user } = renderReview()
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))

      const search = screen.getByRole('combobox')
      await user.click(search)
      await user.keyboard('jks')

      // The letters went into the field, and the cursor did not move.
      expect(search).toHaveValue('jks')
      expect(selectedExternalId()).toBe(FIRST)

      // Enter in the field submits the combobox, never the resolve.
      await user.keyboard('{Enter}')
      expect(screen.queryByText(new RegExp(`Resolved ${FIRST}`))).not.toBeInTheDocument()
    })
  })

  describe('candidate search', () => {
    it('labels the two suggest tiers as two queries, not a fallback chain', async () => {
      const { user } = renderReview()
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))

      await user.type(screen.getByRole('combobox'), 'stal')

      expect(await screen.findByText('Starts with — the as-you-type index')).toBeInTheDocument()
      expect(screen.getByText('Close to it — trigram and edit distance')).toBeInTheDocument()
      const listbox = screen.getByRole('listbox')
      expect(within(listbox).getAllByRole('group')).toHaveLength(2)
    })

    it('is an ARIA 1.2 combobox', async () => {
      const { user } = renderReview()
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))

      const search = screen.getByRole('combobox')
      expect(search).toHaveAttribute('aria-expanded', 'false')
      expect(search).toHaveAccessibleName('Search the catalog for the matching title')

      await user.type(search, 'stal')
      await waitFor(() => expect(search).toHaveAttribute('aria-expanded', 'true'))
      await user.keyboard('{ArrowDown}')
      await waitFor(() => expect(search).toHaveAttribute('aria-activedescendant'))
    })
  })

  describe('loading', () => {
    it('shows a table-shaped skeleton rather than a route spinner', () => {
      renderReview()
      expect(screen.getByText('Loading the review queue …')).toBeInTheDocument()
    })
  })

  describe('empty', () => {
    it('says an empty queue is agreement, not an absence of scanning', async () => {
      server.use(http.get('/admin/unmatched', () => HttpResponse.json(unmatchedEmpty)))
      renderReview()

      expect(await screen.findByText('Nothing is waiting for review')).toBeInTheDocument()
      expect(screen.getByText('Computed, and empty')).toBeInTheDocument()
      expect(screen.getByText('items: [] · next_cursor: null')).toBeInTheDocument()
      expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    })
  })

  describe('error', () => {
    it('shows the queue failure at the code’s scale, with code, status and detail', async () => {
      server.use(
        problemHandler('get', '/admin/unmatched', sourceUnavailable('/admin/unmatched'), {
          retryAfter: 12,
        }),
      )
      renderReview()

      expect(await screen.findByText('Living Room Emby did not answer within 5.0 s.')).toBeInTheDocument()
      expect(screen.getByText('code source_unavailable')).toBeInTheDocument()
      expect(screen.getByText('HTTP 503')).toBeInTheDocument()
      expect(screen.getByText('/admin/unmatched')).toBeInTheDocument()
    })

    it('renders a rejected resolve per field, from errors[].loc and .msg', async () => {
      server.use(
        problemHandler(
          'post',
          '/admin/unmatched/:media_item_id/resolve',
          validationFailed('/admin/unmatched/0191f4c7-ea03-7c31-9038-a71eb72f3b50/resolve'),
        ),
      )
      const { user } = renderReview()
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))

      await user.type(screen.getByRole('combobox'), 'stal')
      const options = await screen.findAllByRole('option', { name: /Stalker/ })
      const first = options[0]
      expect(first).toBeDefined()
      if (first) await user.click(first)

      await user.click(screen.getByRole('button', { name: /Resolve to Stalker/ }))

      expect(await screen.findByText('code validation_failed')).toBeInTheDocument()
      expect(screen.getByText('The request body failed validation.')).toBeInTheDocument()
      expect(screen.getByText('body.base_url')).toBeInTheDocument()
    })
  })

  describe('the trace link (patterns.md §3)', () => {
    function renderFailed(tempoUrl: string | null, traceId?: string) {
      server.use(
        problemHandler(
          'get',
          '/admin/unmatched',
          sourceUnavailable('/admin/unmatched'),
          traceId === undefined ? {} : { traceId },
        ),
      )
      return renderApp(
        withTempo(
          <ToastProvider>
            <Review />
            <ToastStack />
          </ToastProvider>,
          tempoUrl,
        ),
        { theme: 'light', density: 'compact', route: ROUTES.review },
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

  describe('accessibility', () => {
    it('has no axe violations', async () => {
      const { container } = renderReview()
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))
      await expectNoViolations(container)
    })

    it('has no axe violations with the listbox open', async () => {
      const { container, user } = renderReview()
      await waitFor(() => expect(selectedExternalId()).toBe(FIRST))
      await user.type(screen.getByRole('combobox'), 'stal')
      await screen.findAllByRole('option', { name: /Stalker/ })
      await expectNoViolations(container)
    })
  })
})
