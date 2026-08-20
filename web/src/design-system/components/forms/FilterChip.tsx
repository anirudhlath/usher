import { Icon } from '../icon'

/** Toggle chip for browse filters. `tri` mode implements the tri-state `owned` filter — the only
 *  place in the product where a filter has three states, so it prints the state as a word. */
export interface FilterChipProps {
  label: string
  active?: boolean
  /**
   * tri mode only: true | false | undefined
   *
   * `undefined` is a *state* here — "either" — not an absent prop, so it is spelled out in the
   * type. `exactOptionalPropertyTypes` would otherwise refuse the one call site that matters:
   * `value={owned}` where `owned` is the screen's `boolean | undefined`.
   */
  value?: boolean | undefined
  /**
   * The state the chip moves to. A plain chip emits a boolean; `tri` emits
   * `true | false | undefined`, which is why the parameter is widened rather than typed `any`.
   */
  onToggle?: (next: boolean | undefined) => void
  removable?: boolean
  tri?: boolean
}

/**
 * Filter chip. `tri` cycles either → owned → not owned, matching /browse's tri-state owned filter.
 *
 * It is a toggle button, not a checkbox: the contract is `aria-pressed` on a `<button>`, so the
 * chip announces as "pressed" and there is exactly one focusable control per chip — the remove
 * glyph is decoration on the same button, never a nested one.
 *
 * patterns.md §12: the selected state is never colour alone. A pressed chip carries the accent
 * hue **and** the `check` glyph **and** a word — the tri chip prints its state visibly
 * (Owned / Not owned / Either) because three states cannot be read off a border, and the plain
 * chip carries "Selected" for assistive technology alongside `aria-pressed`.
 *
 * Setting or clearing any chip invalidates outstanding cursors; the list restarts silently from
 * the top and `invalid_cursor` is never rendered (patterns.md §3).
 */
export function FilterChip({
  label,
  active = false,
  value,
  onToggle,
  removable = false,
  tri = false,
}: FilterChipProps) {
  if (tri) {
    const state = value === true ? 'Owned' : value === false ? 'Not owned' : 'Either'
    return (
      <button
        type="button"
        className="u-chip u-chip--tri"
        aria-pressed={value !== undefined}
        onClick={() => onToggle?.(value === undefined ? true : value === true ? false : undefined)}
      >
        <span>{label}</span>
        {value !== undefined && <Icon name={value ? 'check' : 'x'} size={16} />}
        <span className="u-chip__state">{state}</span>
      </button>
    )
  }
  return (
    <button type="button" className="u-chip" aria-pressed={active} onClick={() => onToggle?.(!active)}>
      {active && <Icon name="check" size={16} />}
      <span>{label}</span>
      {active && <span className="u-visually-hidden">Selected</span>}
      {removable && active && (
        <span className="u-chip__x" aria-hidden="true">
          <Icon name="x" size={16} />
        </span>
      )}
    </button>
  )
}
