import { useEffect, useRef } from 'react'
import type { InputHTMLAttributes } from 'react'
import clsx from 'clsx'

/** Checkbox or radio (`radio`), with an optional second line of hint text. Supports the
 *  indeterminate state used by the review queue's select-all. */
export interface CheckboxProps extends InputHTMLAttributes<HTMLInputElement> {
  id: string
  label: string
  hint?: string
  indeterminate?: boolean
  radio?: boolean
}

/**
 * Checkbox or radio with an optional hint line.
 *
 * **`indeterminate` is a DOM property, not an attribute** — there is no `indeterminate=""` in
 * HTML, so it is written through a ref after render and re-written whenever the prop changes.
 * It is the review queue's select-all state: some of the page is selected, not all of it.
 *
 * It is *not* a third value. The `owned` filter on /browse is genuinely tri-state and belongs to
 * `FilterChip`'s `tri` mode, which prints its state as a word; a checkbox that means three things
 * is a checkbox nobody can read.
 *
 * The hint is bound with `aria-describedby` rather than swallowed into the accessible name, so
 * the name stays the label the operator was told to look for.
 */
export function Checkbox({
  id,
  label,
  hint,
  indeterminate = false,
  disabled,
  radio = false,
  className,
  ...rest
}: CheckboxProps) {
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate
  }, [indeterminate])

  const labelId = `${id}-label`
  const hintId = hint ? `${id}-hint` : undefined
  const describedBy = [rest['aria-describedby'], hintId].filter(Boolean).join(' ') || undefined

  return (
    <label className={clsx('u-check', disabled && 'u-check--disabled')} htmlFor={id}>
      <input
        {...rest}
        ref={ref}
        id={id}
        type={radio ? 'radio' : 'checkbox'}
        disabled={disabled}
        className={className}
        aria-labelledby={labelId}
        aria-describedby={describedBy}
      />
      <span>
        <span id={labelId}>{label}</span>
        {hint && (
          <span className="u-field__hint u-field__hint--block" id={hintId}>
            {hint}
          </span>
        )}
      </span>
    </label>
  )
}
