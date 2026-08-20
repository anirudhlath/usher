import { describe, expect, it } from 'vitest'
import { Route, Routes } from 'react-router-dom'
import { http, HttpResponse } from 'msw'
import { renderApp, screen, waitFor } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { server } from '@/test/server'
import { person, personWithoutGroups, problemHandler, sourceUnavailable } from '@/test/fixtures'
import { PERSON_DIRECTOR, TITLE_MISSING, TRACE_ID } from '@/test/fixtures/ids'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { ROUTES, personPath } from '@/app/routes'
import Person from './Person'

function renderPerson(personId: string) {
  return renderApp(
    <Routes>
      <Route path={ROUTES.person} element={<Person />} />
    </Routes>,
    { route: personPath(personId) },
  )
}

describe('Person', () => {
  it('renders the filmography under the raw labels the API supplied', async () => {
    const { container } = renderPerson(PERSON_DIRECTOR)

    expect(await screen.findByRole('heading', { level: 1, name: 'Andrei Tarkovsky' })).toBeVisible()

    // "Director" and "Writer" are the record's own spellings. Nothing here
    // relabels, merges or title-cases a group.
    for (const group of person.groups) {
      expect(screen.getByRole('heading', { level: 2, name: group.role })).toBeVisible()
    }
    expect(screen.getByRole('button', { name: 'The Mirror, 1975' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Stalker, 1979' })).toBeVisible()

    await expectNoViolations(container)
  })

  it('states that the API carries no photograph rather than drawing a placeholder', async () => {
    renderPerson(PERSON_DIRECTOR)

    expect(
      await screen.findByText('No photograph, biography or birth year exists for people in this API.'),
    ).toBeVisible()

    // The empty slot is a drawing of an absence: it is not an image, and it is
    // not announced. A placeholder avatar would claim an asset exists.
    expect(screen.queryByRole('img', { name: /andrei/i })).toBeNull()
  })

  it('says the route truncates at 50 credits, because a full-looking page cannot be told apart', async () => {
    renderPerson(PERSON_DIRECTOR)

    const notice = await screen.findByText(/the API caps a person at 50 with no cursor and no total/i)
    expect(notice).toBeVisible()
    expect(notice).toHaveTextContent('This page shows 4 credits')
    expect(screen.getByText('possibly truncated')).toBeVisible()
  })

  it('carries no percentage anywhere: nothing on this screen has a denominator', async () => {
    const { container } = renderPerson(PERSON_DIRECTOR)
    await screen.findByRole('heading', { level: 1, name: 'Andrei Tarkovsky' })

    expect(container.textContent ?? '').not.toContain('%')
    expect(container.textContent ?? '').not.toMatch(/\d+\s*of\s*\d+/)
  })

  it('shows a skeleton shaped like the filmography while the route is pending', async () => {
    const { container } = renderPerson(PERSON_DIRECTOR)

    expect(screen.getByText("Loading this person's filmography …")).toBeInTheDocument()
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()

    await screen.findByRole('heading', { level: 1, name: 'Andrei Tarkovsky' })
  })

  it('renders the absent-groups state with the field that proves it', async () => {
    const { container } = renderPerson(personWithoutGroups.id)

    expect(await screen.findByText('No credits on record')).toBeVisible()
    expect(screen.getByText('groups: absent from payload')).toBeVisible()
    // No group headings, and no truncation notice — there is nothing to truncate.
    expect(screen.queryByRole('heading', { level: 2 })).toBeNull()
    expect(screen.queryByText(/possibly truncated/i)).toBeNull()

    await expectNoViolations(container)
  })

  it('renders a 404 at page scale with back and search, and no retry', async () => {
    const { container } = renderPerson(TITLE_MISSING)

    expect(await screen.findByRole('button', { name: 'Go back' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Search the catalog' })).toBeVisible()
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()

    // code and status in mono, and the server's `detail` verbatim.
    expect(screen.getByText('code not_found')).toBeVisible()
    expect(screen.getByText('HTTP 404')).toBeVisible()
    expect(screen.getByText(`No title with id ${TITLE_MISSING}.`)).toBeVisible()

    await expectNoViolations(container)
  })

  it('reports a transport failure as having no status rather than as a 404', async () => {
    server.use(http.get('/people/:person_id', () => HttpResponse.error()))
    renderPerson(PERSON_DIRECTOR)

    await waitFor(() => {
      expect(screen.getByText("We couldn't reach the server.")).toBeVisible()
    })
    expect(screen.queryByText(/code not_found/)).toBeNull()
  })

  describe('the trace link (patterns.md §3)', () => {
    /**
     * This screen reaches `Problem` through `ScreenProblem`, so this proves the
     * wrapper's wiring on a real screen; `NotFound.test.tsx` carries the full
     * matrix for the wrapper itself.
     */
    function renderFailed(tempoUrl: string | null, traceId?: string) {
      server.use(
        problemHandler(
          'get',
          '/people/:person_id',
          sourceUnavailable('/people/…'),
          traceId === undefined ? {} : { traceId },
        ),
      )
      return renderApp(
        withTempo(
          <Routes>
            <Route path={ROUTES.person} element={<Person />} />
          </Routes>,
          tempoUrl,
        ),
        { route: personPath(PERSON_DIRECTOR) },
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
})
