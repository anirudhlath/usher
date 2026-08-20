import { Fragment, useEffect, useId, useRef, useState, type ReactElement, type ReactNode } from 'react'
import clsx from 'clsx'
import { Button } from '../actions'

/**
 * The confirm pattern for destructive and expensive actions. It names the consequence, the cost and
 * the duration — measured, not hedged. Used for source deletion, bootstrap phases, full syncs and
 * row regeneration.
 *
 * `facts` is the part that matters: download size, measured duration, what it writes, whether it is
 * resumable. All bootstrap phases are resumable and the dialog must say so.
 */
export interface ConfirmFact {
  label: string
  value: string
}

export interface ConfirmDialogProps {
  open: boolean
  title: string
  children?: ReactNode
  facts?: ConfirmFact[]
  confirmLabel?: string
  cancelLabel?: string
  /** Red solid confirm. Only for irreversible destruction. */
  destructive?: boolean
  loading?: boolean
  onConfirm?: () => void
  onCancel?: () => void
  /** Type-to-confirm string (a source name) for the truly irreversible. */
  requireTyped?: string
}

/**
 * The sentence a duration fact carries when this deployment has never measured the action.
 * patterns.md §5: durations are measured, not estimated — an invented range is a lie with a unit
 * on it. Import this rather than retyping it, so every unmeasured fact reads the same.
 */
export const NOT_MEASURED = 'not measured on this deployment'

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/**
 * Open is a mount, not a flag on a permanent tree. That is what makes the typed confirmation reset
 * itself, the trigger get its focus back and the listeners unbind — all from ordinary lifecycle
 * rather than from an effect that watches a boolean.
 */
export function ConfirmDialog(props: ConfirmDialogProps): ReactElement | null {
  if (!props.open) return null
  return <ConfirmDialogBody {...props} />
}

function ConfirmDialogBody({
  title,
  children,
  facts = [],
  confirmLabel = 'Continue',
  cancelLabel = 'Cancel',
  destructive = false,
  loading = false,
  onConfirm,
  onCancel,
  requireTyped,
}: ConfirmDialogProps): ReactElement {
  const [typed, setTyped] = useState('')
  const scrimRef = useRef<HTMLDivElement | null>(null)
  const dialogRef = useRef<HTMLDivElement | null>(null)
  // `Button` is polymorphic over button/anchor, so its ref slot is an `HTMLElement`. Only
  // `.focus()` is called on it here, which every element type has.
  const confirmRef = useRef<HTMLElement | null>(null)
  const titleId = useId()

  /**
   * Focus lands on the confirm button, and goes back to whatever opened the dialog when it closes.
   * Returning focus is the half that gets forgotten: without it a keyboard user restarts from the
   * top of the document every time they cancel.
   */
  useEffect(() => {
    const trigger = document.activeElement
    confirmRef.current?.focus()
    return () => {
      if (trigger instanceof HTMLElement) trigger.focus()
    }
  }, [])

  /**
   * Esc, the scrim click and the Tab trap are bound to the nodes rather than declared as JSX
   * handlers: a scrim and a dialog frame are not interactive elements, and hanging a listener on one
   * in markup is exactly what `jsx-a11y/no-static-element-interactions` is there to catch. The
   * keyboard equivalent of the scrim click is Esc, which is bound here too.
   */
  useEffect(() => {
    const dialog = dialogRef.current
    const scrim = scrimRef.current
    if (!dialog || !scrim) return undefined

    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onCancel?.()
        return
      }
      if (event.key !== 'Tab') return
      const nodes = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE))
      const first = nodes[0]
      const last = nodes[nodes.length - 1]
      if (!first || !last) return
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    const handleMouseDown = (event: MouseEvent): void => {
      if (event.target === scrim) onCancel?.()
    }

    dialog.addEventListener('keydown', handleKeyDown)
    scrim.addEventListener('mousedown', handleMouseDown)
    return () => {
      dialog.removeEventListener('keydown', handleKeyDown)
      scrim.removeEventListener('mousedown', handleMouseDown)
    }
  }, [onCancel])

  const blocked = requireTyped !== undefined && typed !== requireTyped

  return (
    <div className="u-scrim" ref={scrimRef}>
      <div className="u-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} ref={dialogRef}>
        <h2 className="u-dialog__title" id={titleId}>
          {title}
        </h2>
        <div className="u-dialog__body">
          {children}
          {facts.length > 0 && (
            <dl className="u-dialog__facts">
              {facts.map((fact) => (
                <Fragment key={fact.label}>
                  <dt className="u-dialog__k">{fact.label}</dt>
                  {/* An unmeasured duration is prose, not a measurement, so it is not set in mono. */}
                  <dd
                    className={clsx('u-dialog__v', fact.value === NOT_MEASURED && 'u-dialog__v--unmeasured')}
                  >
                    {fact.value}
                  </dd>
                </Fragment>
              ))}
            </dl>
          )}
          {requireTyped !== undefined && (
            <label className="u-field">
              <span className="u-field__label">
                Type <span className="u-dialog__typed">{requireTyped}</span> to confirm
              </span>
              <input
                className="u-input u-input--mono"
                value={typed}
                onChange={(event) => setTyped(event.target.value)}
                autoComplete="off"
              />
            </label>
          )}
        </div>
        <div className="u-dialog__foot">
          <Button type="button" variant="ghost" onClick={onCancel}>
            {cancelLabel}
          </Button>
          {/* Destructive is red; expensive-but-safe is primary. An import is not a deletion.
              `loading` is the spinner, the `aria-busy` and half the refusal; `blocked` is the
              other half, the untyped confirmation, which is a refusal with no spinner. */}
          <Button
            ref={confirmRef}
            type="button"
            variant={destructive ? 'danger-solid' : 'primary'}
            loading={loading}
            disabled={blocked}
            onClick={onConfirm}
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  )
}
