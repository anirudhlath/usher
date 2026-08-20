import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen, within } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { DataTable, type Column } from './index'

type Row = {
  id: string
  external_id: string
  added_at: string
  last: string
  available: boolean
  seen: number
}

const ROWS: Row[] = [
  {
    id: '1',
    external_id: 'emby:4412',
    added_at: '2026-08-14 09:12',
    last: '2026-08-19',
    available: true,
    seen: 12,
  },
  {
    id: '2',
    external_id: 'emby:4419',
    added_at: '2026-08-14 09:12',
    last: '2026-08-19',
    available: true,
    seen: 4,
  },
  {
    id: '3',
    external_id: 'emby:4437',
    added_at: '2026-08-13 22:40',
    last: '2026-08-17',
    available: false,
    seen: 0,
  },
]

const COLUMNS: Column<Row>[] = [
  { key: 'external_id', header: 'External id', mono: true },
  { key: 'added_at', header: 'First seen', mono: true, sortable: true },
  { key: 'last', header: 'Last seen', mono: true },
  { key: 'seen', header: 'Times seen', numeric: true },
  {
    key: 'available',
    header: 'State',
    render: (row) => <span>{row.available ? 'available' : 'missing'}</span>,
  },
]

const CAPTION = 'Unmatched files on Living Room'

describe('DataTable — contract', () => {
  it('renders every row and every column', () => {
    renderComponent(<DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} />)
    expect(screen.getAllByRole('row')).toHaveLength(ROWS.length + 1)
    expect(screen.getAllByRole('columnheader')).toHaveLength(COLUMNS.length)
    expect(screen.getByText('emby:4437')).toBeInTheDocument()
  })

  it('marks mono and numeric cells for the stylesheet', () => {
    const { container } = renderComponent(<DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} />)
    const firstCell = container.querySelector('tbody tr td')
    expect(firstCell).toHaveAttribute('data-mono', 'true')
    expect(firstCell).not.toHaveAttribute('data-num')
    const numeric = screen.getByText('12').closest('td')
    expect(numeric).toHaveAttribute('data-num', 'true')
  })

  it('uses a column’s render function when it has one', () => {
    renderComponent(<DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} />)
    expect(screen.getAllByText('available')).toHaveLength(2)
    expect(screen.getByText('missing')).toBeInTheDocument()
  })

  it('keys rows off `keyField`', () => {
    const { container } = renderComponent(
      <DataTable
        caption={CAPTION}
        columns={COLUMNS}
        rows={ROWS}
        keyField="external_id"
        selectedId="emby:4419"
      />,
    )
    expect(container.querySelector('tr[aria-selected="true"]')).toHaveTextContent('emby:4419')
  })

  it('shows a sentence, not "No data", when there is nothing to show', () => {
    const message = 'Nothing is waiting for review. Every file on this source matched a catalog title.'
    const { container } = renderComponent(
      <DataTable caption={CAPTION} columns={COLUMNS} rows={[]} emptyMessage={message} />,
    )
    expect(screen.getByText(message)).toHaveClass('u-table__empty')
    expect(container.querySelector('table')).toBeNull()
  })

  it('renders an em dash for a cell the payload does not carry', () => {
    const rows = [{ id: '1', external_id: 'emby:1', added_at: '', last: '', available: true, seen: 1 }]
    const columns: Column<Record<string, unknown>>[] = [{ key: 'missing_key', header: 'Absent' }]
    renderComponent(<DataTable caption={CAPTION} columns={columns} rows={rows} />)
    expect(screen.getByRole('cell')).toHaveTextContent('—')
  })
})

describe('DataTable — sorting', () => {
  it('marks a sortable column that is not the sort key as aria-sort="none"', () => {
    renderComponent(
      <DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} onSort={vi.fn<() => void>()} />,
    )
    expect(screen.getByRole('columnheader', { name: 'First seen' })).toHaveAttribute('aria-sort', 'none')
  })

  it.each([
    ['asc', 'ascending'],
    ['desc', 'descending'],
  ] as const)('reflects sort direction %s as aria-sort="%s"', (dir, expected) => {
    renderComponent(
      <DataTable
        caption={CAPTION}
        columns={COLUMNS}
        rows={ROWS}
        sort={{ key: 'added_at', dir }}
        onSort={vi.fn<() => void>()}
      />,
    )
    expect(screen.getByRole('columnheader', { name: 'First seen' })).toHaveAttribute('aria-sort', expected)
  })

  it('leaves aria-sort off a column that cannot be sorted', () => {
    renderComponent(
      <DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} onSort={vi.fn<() => void>()} />,
    )
    expect(screen.getByRole('columnheader', { name: 'Last seen' })).not.toHaveAttribute('aria-sort')
  })

  it('sorts from a real button, so the header is reachable by keyboard', async () => {
    const onSort = vi.fn<() => void>()
    const { user } = renderComponent(
      <DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} onSort={onSort} />,
    )
    const header = screen.getByRole('columnheader', { name: 'First seen' })
    await user.click(within(header).getByRole('button', { name: 'First seen' }))
    expect(onSort).toHaveBeenCalledExactlyOnceWith('added_at')
  })
})

describe('DataTable — keyboard and selection (§9, §12)', () => {
  it('opens a row on click', async () => {
    const onRowClick = vi.fn<() => void>()
    const { user } = renderComponent(
      <DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} onRowClick={onRowClick} />,
    )
    await user.click(screen.getByRole('row', { name: /emby:4419/ }))
    expect(onRowClick).toHaveBeenCalledExactlyOnceWith(ROWS[1])
  })

  it('opens the focused row on Enter', async () => {
    const onRowClick = vi.fn<() => void>()
    const { user } = renderComponent(
      <DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} onRowClick={onRowClick} />,
    )
    screen.getByRole('row', { name: /emby:4437/ }).focus()
    await user.keyboard('{Enter}')
    expect(onRowClick).toHaveBeenCalledExactlyOnceWith(ROWS[2])
  })

  it('moves row focus with ↓ and ↑', async () => {
    const { user } = renderComponent(
      <DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} onRowClick={vi.fn<() => void>()} />,
    )
    const first = screen.getByRole('row', { name: /emby:4412/ })
    const second = screen.getByRole('row', { name: /emby:4419/ })
    const third = screen.getByRole('row', { name: /emby:4437/ })

    first.focus()
    await user.keyboard('{ArrowDown}')
    expect(second).toHaveFocus()
    await user.keyboard('{ArrowDown}')
    expect(third).toHaveFocus()
    await user.keyboard('{ArrowUp}')
    expect(second).toHaveFocus()
  })

  it('does not wrap past either end', async () => {
    const { user } = renderComponent(
      <DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} onRowClick={vi.fn<() => void>()} />,
    )
    const first = screen.getByRole('row', { name: /emby:4412/ })
    first.focus()
    await user.keyboard('{ArrowUp}')
    expect(first).toHaveFocus()
  })

  it('is one tab stop, not one per row', () => {
    renderComponent(
      <DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} onRowClick={vi.fn<() => void>()} />,
    )
    const rows = screen.getAllByRole('row').slice(1)
    expect(rows.map((row) => row.getAttribute('tabindex'))).toEqual(['0', '-1', '-1'])
  })

  it('puts the tab stop on the selected row', () => {
    renderComponent(
      <DataTable
        caption={CAPTION}
        columns={COLUMNS}
        rows={ROWS}
        selectedId="3"
        onRowClick={vi.fn<() => void>()}
      />,
    )
    const rows = screen.getAllByRole('row').slice(1)
    expect(rows.map((row) => row.getAttribute('tabindex'))).toEqual(['-1', '-1', '0'])
  })

  it('marks the selected row with aria-selected', () => {
    renderComponent(<DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} selectedId="2" />)
    const rows = screen.getAllByRole('row').slice(1)
    expect(rows.map((row) => row.getAttribute('aria-selected'))).toEqual(['false', 'true', 'false'])
  })

  it('states no selection at all when the table has no selection model', () => {
    renderComponent(<DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} />)
    for (const row of screen.getAllByRole('row')) {
      expect(row).not.toHaveAttribute('aria-selected')
    }
  })

  it('leaves rows unfocusable when there is nothing to open', () => {
    renderComponent(<DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} />)
    for (const row of screen.getAllByRole('row').slice(1)) {
      expect(row).not.toHaveAttribute('tabindex')
    }
  })
})

describe('DataTable — asCards is a markup switch, not a reflow (§11)', () => {
  it('renders a real table by default', () => {
    renderComponent(<DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} />)
    expect(screen.getByRole('table', { name: CAPTION })).toBeInTheDocument()
  })

  it('renders no table element at all as cards', () => {
    const { container } = renderComponent(
      <DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} asCards />,
    )
    expect(container.querySelector('table')).toBeNull()
    expect(screen.queryByRole('table')).toBeNull()
    expect(container.querySelectorAll('.u-tcard')).toHaveLength(ROWS.length)
  })

  it('carries the same facts as key/value pairs, each labelled by its column header', () => {
    renderComponent(<DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} asCards />)
    expect(screen.getAllByText('External id')).toHaveLength(ROWS.length)
    expect(screen.getByText('emby:4437')).toBeInTheDocument()
    expect(screen.getByText('missing')).toBeInTheDocument()
  })

  it('keeps the caption as the group’s accessible name', () => {
    renderComponent(<DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} asCards />)
    expect(screen.getByRole('group', { name: CAPTION })).toBeInTheDocument()
  })

  it('opens a card on click', async () => {
    const onRowClick = vi.fn<() => void>()
    const { user } = renderComponent(
      <DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} asCards onRowClick={onRowClick} />,
    )
    await user.click(screen.getByRole('button', { name: /emby:4419/ }))
    expect(onRowClick).toHaveBeenCalledExactlyOnceWith(ROWS[1])
  })

  it('moves card focus with ↓ and ↑', async () => {
    const { user } = renderComponent(
      <DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} asCards onRowClick={vi.fn<() => void>()} />,
    )
    const first = screen.getByRole('button', { name: /emby:4412/ })
    const second = screen.getByRole('button', { name: /emby:4419/ })
    first.focus()
    await user.keyboard('{ArrowDown}')
    expect(second).toHaveFocus()
    await user.keyboard('{ArrowUp}')
    expect(first).toHaveFocus()
  })

  it('states selection with aria-current, which a button supports', () => {
    renderComponent(
      <DataTable
        caption={CAPTION}
        columns={COLUMNS}
        rows={ROWS}
        asCards
        selectedId="2"
        onRowClick={vi.fn<() => void>()}
      />,
    )
    expect(screen.getByRole('button', { name: /emby:4419/ })).toHaveAttribute('aria-current', 'true')
    expect(screen.getByRole('button', { name: /emby:4412/ })).not.toHaveAttribute('aria-current')
  })

  it('renders static cards when there is nothing to open', () => {
    const { container } = renderComponent(
      <DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} asCards />,
    )
    expect(container.querySelectorAll('button.u-tcard')).toHaveLength(0)
    expect(container.querySelectorAll('div.u-tcard')).toHaveLength(ROWS.length)
  })

  it('shows the same empty sentence in either mode', () => {
    const { rerender } = renderComponent(
      <DataTable caption={CAPTION} columns={COLUMNS} rows={[]} emptyMessage="Nothing here yet." />,
    )
    expect(screen.getByText('Nothing here yet.')).toBeInTheDocument()
    rerender(
      <DataTable caption={CAPTION} columns={COLUMNS} rows={[]} emptyMessage="Nothing here yet." asCards />,
    )
    expect(screen.getByText('Nothing here yet.')).toBeInTheDocument()
  })
})

describe('DataTable — accessibility (§12: tables)', () => {
  it('has a caption, visually hidden, that names the table', () => {
    const { container } = renderComponent(<DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} />)
    const caption = container.querySelector('caption')
    expect(caption).toHaveTextContent(CAPTION)
    expect(caption).toHaveClass('u-visually-hidden')
  })

  it('scopes every header to its column', () => {
    renderComponent(<DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} />)
    for (const header of screen.getAllByRole('columnheader')) {
      expect(header).toHaveAttribute('scope', 'col')
    }
  })

  it('has no axe violations as a table', async () => {
    const { container } = renderComponent(
      <DataTable
        caption={CAPTION}
        columns={COLUMNS}
        rows={ROWS}
        selectedId="2"
        sort={{ key: 'added_at', dir: 'desc' }}
        onSort={vi.fn<() => void>()}
        onRowClick={vi.fn<() => void>()}
      />,
      { theme: 'light', density: 'compact' },
    )
    await expectNoViolations(container)
  })

  it('has no axe violations as cards', async () => {
    const { container } = renderComponent(
      <DataTable
        caption={CAPTION}
        columns={COLUMNS}
        rows={ROWS}
        asCards
        selectedId="2"
        onRowClick={vi.fn<() => void>()}
      />,
      { theme: 'light', density: 'compact' },
    )
    await expectNoViolations(container)
  })

  it('has no axe violations when empty', async () => {
    const { container } = renderComponent(<DataTable caption={CAPTION} columns={COLUMNS} rows={[]} />, {
      theme: 'light',
      density: 'compact',
    })
    await expectNoViolations(container)
  })
})

describe('DataTable — anti-patterns (§4: keyset only)', () => {
  it('renders no pager, no page numbers and no jump-to-page', () => {
    const { container } = renderComponent(
      <DataTable
        caption={CAPTION}
        columns={COLUMNS}
        rows={ROWS}
        onRowClick={vi.fn<() => void>()}
        onSort={vi.fn<() => void>()}
      />,
    )
    expect(screen.queryByRole('navigation')).toBeNull()
    expect(screen.queryByRole('button', { name: /next|previous|page \d+/i })).toBeNull()
    expect(container.textContent).not.toMatch(/page \d+|\d+ of \d+|showing \d+/i)
  })

  it('renders no total and no result count', () => {
    const { container } = renderComponent(<DataTable caption={CAPTION} columns={COLUMNS} rows={ROWS} />)
    expect(container.textContent).not.toMatch(/\b3 results?\b|\btotal\b/i)
  })
})
