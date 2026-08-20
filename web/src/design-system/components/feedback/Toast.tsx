import type { ReactElement, ReactNode } from 'react'
import clsx from 'clsx'
import { Icon } from '../icon'

/**
 * "Queued, not done." Every mutating admin action returns `202 {kind, key}` — and there is
 * currently no route to look that key up (see REQUIRES BACKEND WORK). So the toast states exactly
 * what was queued, prints the key in mono, says whether it coalesced with an existing job, and
 * points at where the result will show up.
 */
export interface ToastProps {
  tone?: 'info' | 'good' | 'warn' | 'bad'
  /** "Queued a full sync of Living Room." */
  title: string
  children?: ReactNode
  /** The `key` from the 202 body, printed in mono and copyable. */
  jobKey?: string
  coalesced?: boolean
  /** Usually a TextLink to Pipeline or Bootstrap. */
  action?: ReactNode
  onDismiss?: () => void
  icon?: ReactNode
}

/**
 * A 202 receipt has no timer. It persists until dismissed, because it is the only record of a key
 * that nothing can look up — a toast that faded would take the evidence with it.
 */
export function Toast({
  tone = 'info',
  title,
  children,
  jobKey,
  coalesced,
  action,
  onDismiss,
  icon,
}: ToastProps): ReactElement {
  return (
    <div className="u-toast" role="status" aria-live="polite">
      {icon && <span className={clsx('u-toast__icon', `u-toast__icon--${tone}`)}>{icon}</span>}
      <span className="u-toast__body">
        <span className="u-toast__title">{title}</span>
        {children && <span className="u-toast__text">{children}</span>}
        {/* Stated only when known: an absent `coalesced` means the server did not say. */}
        {coalesced === true && (
          <span className="u-toast__text u-toast__text--warn">
            It coalesced with a job already running — nothing new was started.
          </span>
        )}
        {/* Selectable prose, never a button: an operator pastes this into a log search. */}
        {jobKey && <span className="u-toast__key">key {jobKey}</span>}
        {action}
      </span>
      {onDismiss && (
        <button type="button" className="u-iconbtn u-iconbtn--sm" aria-label="Dismiss" onClick={onDismiss}>
          <Icon name="x" size={16} />
        </button>
      )}
    </div>
  )
}

export interface ToastStackProps {
  children?: ReactNode
}

export function ToastStack({ children }: ToastStackProps): ReactElement {
  return (
    <div className="u-toasts" role="region" aria-label="Notifications">
      {children}
    </div>
  )
}
