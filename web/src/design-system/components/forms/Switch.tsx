import type { KeyboardEvent } from 'react'

/** A real `role="switch"` with a bound label and optional description. The row-provider list is the
 *  main consumer: ten switches, each explaining an opaque slug in plain language. */
export interface SwitchProps {
  id: string
  checked: boolean
  onChange?: (next: boolean) => void
  /** Required. An unnamed switch was one of the reference client's measured a11y failures. */
  label: string
  /** Plain-language explanation. For row providers this is where the slug gets translated. */
  description?: string
  disabled?: boolean
}

/**
 * Toggle with a required accessible name. Used for the ten row providers.
 *
 * patterns.md §12 asks for four things and this is all four: a real `role="switch"`, an
 * `aria-checked` that carries the state, a label bound by id, and a `description` that explains
 * an opaque provider slug in plain language. The row-providers screen is the reason the
 * description exists — `franchise-completion` is a database value, not a sentence, and a switch
 * whose only name is a slug is a switch an operator toggles by guesswork.
 *
 * `Space` and `Enter` both toggle, and both suppress the default so the page does not scroll
 * under the operator.
 */
export function Switch({ checked, onChange, label, description, disabled = false, id }: SwitchProps) {
  const labelId = `${id}-label`
  const descId = description ? `${id}-desc` : undefined

  const toggle = () => {
    if (!disabled) onChange?.(!checked)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLSpanElement>) => {
    if (disabled) return
    if (e.key === ' ' || e.key === 'Enter') {
      e.preventDefault()
      onChange?.(!checked)
    }
  }

  return (
    <span
      role="switch"
      id={id}
      tabIndex={disabled ? -1 : 0}
      aria-checked={checked}
      aria-disabled={disabled || undefined}
      aria-labelledby={labelId}
      aria-describedby={descId}
      className="u-switch"
      onClick={toggle}
      onKeyDown={onKeyDown}
    >
      <span className="u-switch__track">
        <span className="u-switch__knob" />
      </span>
      <span>
        <span className="u-switch__label" id={labelId}>
          {label}
        </span>
        {description && (
          <span className="u-field__hint u-field__hint--block" id={descId}>
            {description}
          </span>
        )}
      </span>
    </span>
  )
}
