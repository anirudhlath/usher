import { useState } from 'react'
import { Badge, DataTable, LoadMore, type Column } from '@/design-system'
import { GroupSection, Specimen } from '../Specimen'

/**
 * A `type` rather than an `interface`: `DataTable`'s row is constrained to
 * `Record<string, unknown>`, and only a type alias gets the implicit index
 * signature that satisfies it. No cast is needed either way.
 */
type UnmatchedRow = {
  id: string
  external_id: string
  added_at: string
  last_seen: string
  available: boolean
}

/** Frozen. Every timestamp is a literal — nothing here is computed from a clock. */
const ROWS: UnmatchedRow[] = [
  {
    id: '1',
    external_id: 'emby:4412',
    added_at: '2026-08-14 09:12',
    last_seen: '2026-08-19',
    available: true,
  },
  {
    id: '2',
    external_id: 'emby:4419',
    added_at: '2026-08-14 09:12',
    last_seen: '2026-08-19',
    available: true,
  },
  {
    id: '3',
    external_id: 'emby:4437',
    added_at: '2026-08-13 22:40',
    last_seen: '2026-08-17',
    available: false,
  },
]

const COLUMNS: Column<UnmatchedRow>[] = [
  { key: 'external_id', header: 'External id', mono: true },
  { key: 'added_at', header: 'First seen', mono: true, sortable: true },
  { key: 'last_seen', header: 'Last seen', mono: true },
  {
    key: 'available',
    header: 'State',
    render: (row) => (
      <Badge tone={row.available ? 'good' : 'warn'}>{row.available ? 'available' : 'missing'}</Badge>
    ),
  },
]

/** Sorting is local and starts from a fixed key and direction. */
function LiveTable({ asCards = false }: { asCards?: boolean }) {
  const [sort, setSort] = useState<{ key: string; dir: 'asc' | 'desc' }>({ key: 'added_at', dir: 'desc' })
  const [selectedId, setSelectedId] = useState('2')
  return (
    <DataTable
      caption="Unmatched files on Living Room"
      keyField="id"
      rows={ROWS}
      columns={COLUMNS}
      sort={sort}
      onSort={(key) => setSort((current) => ({ key, dir: current.dir === 'asc' ? 'desc' : 'asc' }))}
      selectedId={selectedId}
      onRowClick={(row) => setSelectedId(row.id)}
      asCards={asCards}
    />
  )
}

export function DataSpecimens() {
  return (
    <GroupSection
      id="data"
      title="Data"
      blurb="The operator table and the one pagination idiom. No page numbers, no totals, no result counts anywhere — the API returns items and an opaque cursor, and a silent stop is indistinguishable from a bug, so the end of a list is a sentence."
    >
      <Specimen
        name="DataTable/rows"
        wide
        note="Sticky head, mono identifier cells, aria-sort on the sortable header, aria-selected on the selected row. Operable: click a row, click First seen to sort."
      >
        <div className="k-fill">
          <LiveTable />
        </div>
      </Specimen>

      <Specimen
        name="DataTable/as-cards"
        wide
        note="§11: a table must not shrink its type to fit a phone. The markup changes instead — the same rows as stacked key/value cards."
      >
        <div className="k-fill">
          <LiveTable asCards />
        </div>
      </Specimen>

      <Specimen
        name="DataTable/static"
        wide
        note="No onRowClick: the rows are not focusable and the table is not interactive."
      >
        <div className="k-fill">
          <DataTable caption="Sources" keyField="id" rows={ROWS} columns={COLUMNS} />
        </div>
      </Specimen>

      <Specimen
        name="DataTable/empty"
        wide
        note="A sentence that says which of empty, never computed or unavailable this is — never “No data”."
      >
        <div className="k-fill">
          <DataTable
            caption="Unmatched files on Living Room"
            keyField="id"
            rows={[]}
            columns={COLUMNS}
            emptyMessage="Nothing is waiting for review. Every file on this source matched a catalog title."
          />
        </div>
      </Specimen>

      <Specimen
        name="LoadMore/more"
        width={420}
        note="“3 loaded so far” counts what is loaded, never what remains. There is no denominator."
      >
        <div className="k-fill">
          <LoadMore nextCursor="eyJvIjoyNDAwfQ" loadedLabel="3 loaded so far" />
        </div>
      </Specimen>

      <Specimen name="LoadMore/loading" width={420}>
        <div className="k-fill">
          <LoadMore nextCursor="eyJvIjoyNDAwfQ" loading loadedLabel="3 loaded so far" />
        </div>
      </Specimen>

      <Specimen
        name="LoadMore/end"
        width={420}
        note="next_cursor === null. The sentence is the point: a list that just stops looks like a bug."
      >
        <div className="k-fill">
          <LoadMore nextCursor={null} />
        </div>
      </Specimen>

      <Specimen
        name="LoadMore/auto-load"
        width={420}
        note="Viewer grids auto-load 600 px before the sentinel. The button is rendered in both modes, so the last page stays reachable from the keyboard when the observer never fires."
      >
        <div className="k-fill">
          <LoadMore nextCursor="eyJvIjoyNDAwfQ" autoLoad loadedLabel="72 loaded so far" />
        </div>
      </Specimen>
    </GroupSection>
  )
}
