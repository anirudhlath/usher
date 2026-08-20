import { isValidElement, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'

/**
 * The operator table. Six-to-ten columns at desktop, the same rows as stacked key/value cards on
 * phone (`asCards`). Sticky header, hairline rows, mono identifier columns, right-aligned numerics.
 *
 * There are no page numbers, no totals and no result counts anywhere in this product — pair it with
 * LoadMore, never with a pager.
 */
export interface Column<T = Record<string, unknown>> {
  key: string
  header: string
  /** Mono face + tabular numerals — for ids, cursors, timestamps, codecs. */
  mono?: boolean
  /** Right-aligned tabular numerals. */
  numeric?: boolean
  sortable?: boolean
  render?: (row: T) => ReactNode
}

export interface DataTableProps<T = Record<string, unknown>> {
  columns: Column<T>[]
  rows: T[]
  /** Visually hidden caption. Required for any table an operator reads with a screen reader. */
  caption?: string
  keyField?: string
  selectedId?: string
  onRowClick?: (row: T) => void
  /** A sentence, not "No data". */
  emptyMessage?: string
  /** Phone: render as stacked cards instead of a horizontally scrolling table. */
  asCards?: boolean
  sort?: { key: string; dir: 'asc' | 'desc' }
  onSort?: (key: string) => void
}

/**
 * `T = any` in the handoff's contract; `any` is banned here, so the row is a record of
 * `unknown` and every cell goes through `toNode`. A column that holds anything other than
 * a primitive supplies `render`.
 */
function toNode(value: unknown): ReactNode {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string' || typeof value === 'number') return value
  if (isValidElement(value)) return value
  return String(value)
}

/** Operator table. Sticky head, hairline rows, mono identifier cells, right-aligned numerics.
 *  With `asCards` the same data renders as stacked key/value cards — patterns.md §11 forbids
 *  shrinking a table's type to fit a phone, so the markup changes rather than the font size. */
export function DataTable<T extends Record<string, unknown>>({
  columns,
  rows,
  caption,
  keyField = 'id',
  selectedId,
  onRowClick,
  emptyMessage = 'Nothing here yet.',
  asCards = false,
  sort,
  onSort,
}: DataTableProps<T>) {
  const rootRef = useRef<HTMLDivElement>(null)
  const [focusedIndex, setFocusedIndex] = useState<number | null>(null)

  const rowKey = (row: T, index: number): string => {
    const raw = row[keyField]
    return typeof raw === 'string' || typeof raw === 'number' ? String(raw) : String(index)
  }

  const selectedIndex = rows.findIndex((row, index) => rowKey(row, index) === selectedId)
  const interactive = onRowClick !== undefined
  const tabStop = Math.min(
    focusedIndex ?? (selectedIndex >= 0 ? selectedIndex : 0),
    Math.max(rows.length - 1, 0),
  )

  /** §9: `↑`/`↓` move row focus. Focus is roving, so a table is one tab stop, not one per row. */
  const moveFocus = (event: KeyboardEvent<HTMLElement>, index: number): boolean => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return false
    const next = index + (event.key === 'ArrowDown' ? 1 : -1)
    if (next < 0 || next >= rows.length) return true
    event.preventDefault()
    const target = rootRef.current?.querySelector<HTMLElement>(`[data-row-index="${next}"]`)
    if (target) {
      setFocusedIndex(next)
      target.focus()
    }
    return true
  }

  const cell = (row: T, column: Column<T>): ReactNode =>
    column.render ? column.render(row) : toNode(row[column.key])

  if (rows.length === 0) {
    return (
      <div className="u-table__wrap">
        <div className="u-table__empty">{emptyMessage}</div>
      </div>
    )
  }

  if (asCards) {
    return (
      <div className="u-table__cards" ref={rootRef} role="group" aria-label={caption}>
        {rows.map((row, index) => {
          const key = rowKey(row, index)
          const selected = selectedId !== undefined && key === selectedId
          const body = columns.map((column) => (
            <span className="u-tcard__row" key={column.key}>
              <span className="u-tcard__k">{column.header}</span>
              <span className="u-tcard__v" data-mono={column.mono || undefined}>
                {cell(row, column)}
              </span>
            </span>
          ))

          if (!interactive) {
            return (
              <div className="u-tcard" key={key} data-row-index={index}>
                {body}
              </div>
            )
          }

          return (
            <button
              type="button"
              className="u-tcard"
              key={key}
              data-row-index={index}
              /* `aria-selected` is not a supported property of `button`; the card fallback
                 states the same fact with `aria-current`, which is. */
              aria-current={selected || undefined}
              tabIndex={index === tabStop ? 0 : -1}
              onClick={() => {
                setFocusedIndex(index)
                onRowClick(row)
              }}
              onKeyDown={(event) => {
                moveFocus(event, index)
              }}
            >
              {body}
            </button>
          )
        })}
      </div>
    )
  }

  return (
    /*
      **`tabIndex={0}` on the scroll container, and it is a WCAG 2.1.1 requirement
      rather than a nicety.** `.u-table__wrap` is `overflow:auto`, and at 390 px a
      six-column operator table overflows it (measured: scrollWidth 431 against
      clientWidth 330). A region that scrolls and cannot be focused is content a
      keyboard user cannot reach at all — there is nothing to put the caret in and
      no arrow key that moves it. axe reports it as `scrollable-region-focusable`,
      and it failed all four phone-390 sweeps.

      `role="group"` with the caption as its name, because a bare focusable `div`
      is a tab stop that announces nothing; this way it announces the same name the
      `<caption>` carries. The card fallback already did exactly this — it is the
      table branch that had been left behind.

      Harmless above 390: with no overflow the element still takes focus but there
      is nothing to scroll, which is the same behaviour every browser gives a
      scrollable region that happens to fit.
    */
    <div className="u-table__wrap" ref={rootRef} tabIndex={0} role="group" aria-label={caption}>
      <table className="u-table">
        {caption ? <caption className="u-visually-hidden">{caption}</caption> : null}
        <thead>
          <tr>
            {columns.map((column) => {
              const ariaSort = column.sortable
                ? sort && sort.key === column.key
                  ? sort.dir === 'asc'
                    ? 'ascending'
                    : 'descending'
                  : 'none'
                : undefined
              return (
                <th key={column.key} scope="col" aria-sort={ariaSort}>
                  {column.sortable && onSort ? (
                    <button type="button" className="u-table__sort" onClick={() => onSort(column.key)}>
                      {column.header}
                    </button>
                  ) : (
                    column.header
                  )}
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const key = rowKey(row, index)
            const selected = selectedId !== undefined && key === selectedId
            return (
              <tr
                key={key}
                data-row-index={index}
                tabIndex={interactive ? (index === tabStop ? 0 : -1) : undefined}
                aria-selected={selectedId !== undefined ? selected : undefined}
                onClick={
                  onRowClick
                    ? () => {
                        setFocusedIndex(index)
                        onRowClick(row)
                      }
                    : undefined
                }
                onKeyDown={
                  onRowClick
                    ? (event) => {
                        if (moveFocus(event, index)) return
                        if (event.key === 'Enter') {
                          event.preventDefault()
                          onRowClick(row)
                        }
                      }
                    : undefined
                }
              >
                {columns.map((column) => (
                  <td
                    key={column.key}
                    data-mono={column.mono || undefined}
                    data-num={column.numeric || undefined}
                  >
                    {cell(row, column)}
                  </td>
                ))}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
