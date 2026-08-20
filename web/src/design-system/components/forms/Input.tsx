import { createElement } from 'react'
import type { InputHTMLAttributes, ReactNode } from 'react'
import clsx from 'clsx'
import { Icon, STATE_ICON } from '../icon'

/**
 * Text input plus its label, hint, error and aria wiring. `error` takes the `msg` from a
 * validation_failed problem document's `errors[].msg` verbatim.
 *
 */
export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** Required — the label is bound to it. */
  id: string
  label?: string
  /** Sits under the field until an error replaces it. */
  hint?: string
  /** Field-scale error. Sets aria-invalid and a polite live region. */
  error?: string
  /** Mono face — use for base_url, device_id, external_id, any identifier. */
  mono?: boolean
  textarea?: boolean
  lead?: ReactNode
  trail?: ReactNode
}

/**
 * Text input with the field furniture attached: label, hint, error, and aria wiring.
 *
 * Three rules are load-bearing rather than cosmetic:
 *
 * - **`error` is printed verbatim.** It is `errors[].msg` from a 422 `validation_failed`
 *   problem document (patterns.md §3), matched to this field by `errors[].loc`. Never reword
 *   it, never parse it, never synthesise one.
 * - **The error carries hue, icon and word** (patterns.md §12) — the `x-circle` glyph is fixed
 *   for the bad tone, so colour is never the only thing that says the field is wrong.
 * - **A password value is write-only** (patterns.md §13). Nothing here reads the value back
 *   into markup: it is never echoed into a hint, a label, a title or an aria attribute, and
 *   `type="password"` reaches the DOM untouched so the browser masks it. The hint is where the
 *   field states where the credential goes — "stored encrypted on the server, never returned by
 *   the API".
 */
export function Input({
  id,
  label,
  hint,
  error,
  mono = false,
  textarea = false,
  lead,
  trail,
  required,
  className,
  ...rest
}: InputProps) {
  // The hint is replaced by the error rather than stacked under it, so its id may only be
  // referenced while it is actually on screen — an `aria-describedby` pointing at markup that
  // was not rendered is a dangling reference, and screen readers announce nothing for it.
  const showHint = Boolean(hint) && !error
  const hintId = showHint ? `${id}-hint` : undefined
  const errId = error ? `${id}-error` : undefined
  const describedBy = [rest['aria-describedby'], hintId, errId].filter(Boolean).join(' ') || undefined

  // `textarea` swaps the tag and nothing else, which is the one place the contract's single
  // props interface meets two DOM elements: every handler on `InputHTMLAttributes` is typed to
  // `HTMLInputElement`, so JSX would reject the same object on a `<textarea>`. `createElement`
  // takes the tag as a value and keeps the contract verbatim — no cast, no second interface.
  const control = createElement(textarea ? 'textarea' : 'input', {
    ...rest,
    id,
    className: clsx('u-input', mono && 'u-input--mono', textarea && 'u-input--textarea', className),
    'aria-invalid': error ? true : undefined,
    'aria-describedby': describedBy,
    // `required` becomes `aria-required` only. The native attribute would hand validation to
    // the browser's own bubbles; in this product the field-scale error is the server's
    // `validation_failed` message, printed verbatim (patterns.md §3).
    'aria-required': required || undefined,
  })

  return (
    <div className="u-field">
      {label && (
        <label className="u-field__label" htmlFor={id}>
          {label}
        </label>
      )}
      {lead || trail ? (
        <div className={clsx('u-inputwrap', lead && 'u-inputwrap--lead', trail && 'u-inputwrap--trail')}>
          {lead && (
            <span className="u-inputwrap__lead" aria-hidden="true">
              {lead}
            </span>
          )}
          {control}
          {trail && (
            <span className="u-inputwrap__trail" aria-hidden="true">
              {trail}
            </span>
          )}
        </div>
      ) : (
        control
      )}
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
