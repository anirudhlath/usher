import type { ChangeEvent, SelectHTMLAttributes } from 'react'
import clsx from 'clsx'
import { Icon, STATE_ICON } from '../icon'

export interface SelectOption {
  value: string
  label: string
}

/** Native select, styled. Used for browse sort (name | year | popularity | vote_count) and search
 *  mode (full_text | semantic | fused). Native because it is correct on every device for free. */
export interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, 'children'> {
  id: string
  label?: string
  hint?: string
  error?: string
  options: SelectOption[]
  value?: string
  onChange?: (e: ChangeEvent<HTMLSelectElement>) => void
  disabled?: boolean
}

/**
 * A real `<select>`, styled — not a custom listbox. Native is correct on every device for free,
 * and the option set here is short and fixed.
 *
 * The name comes from a bound `<label>`, never from a placeholder option: a placeholder-as-label
 * disappears the moment a value is chosen, which is precisely when a returning operator needs to
 * know what the control governs.
 *
 * Changing a select that feeds a keyset list invalidates any outstanding cursor. The list restarts
 * from the top silently — `invalid_cursor` is never rendered (patterns.md §3).
 */
export function Select({ id, label, hint, error, options, className, ...rest }: SelectProps) {
  // The hint is replaced by the error, so its id is only referenced while it is on screen.
  const showHint = Boolean(hint) && !error
  const hintId = showHint ? `${id}-hint` : undefined
  const errId = error ? `${id}-error` : undefined
  const describedBy = [rest['aria-describedby'], hintId, errId].filter(Boolean).join(' ') || undefined

  return (
    <div className="u-field">
      {label && (
        <label className="u-field__label" htmlFor={id}>
          {label}
        </label>
      )}
      <span className="u-select__wrap">
        <select
          {...rest}
          id={id}
          className={clsx('u-input', 'u-select', className)}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <span className="u-select__chev" aria-hidden="true">
          <Icon name="chevron-down" size={16} />
        </span>
      </span>
      {showHint && (
        <span className="u-field__hint" id={hintId}>
          {hint}
        </span>
      )}
      {error && (
        <span className="u-field__error" id={errId} role="status">
          <Icon name={STATE_ICON.bad} size={16} />
          {error}
        </span>
      )}
    </div>
  )
}
