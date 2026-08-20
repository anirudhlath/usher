import type {
  ButtonHTMLAttributes,
  HTMLAttributeAnchorTarget,
  MouseEvent as ReactMouseEvent,
  ReactNode,
  Ref,
} from 'react'
import { useCallback } from 'react'
import clsx from 'clsx'

/**
 * The action primitive. Monochrome primary, outlined secondary, borderless ghost, two danger
 * treatments. Loading keeps the button's width and swaps in a spinner plus an optional pending
 * label — use it for /play (measured 1–5 s) and every probe.
 *
 */
export interface ButtonProps extends ButtonHTMLAttributes<HTMLElement> {
  /** primary = monochrome high contrast · danger-solid only for irreversible destruction */
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger' | 'danger-solid'
  size?: 'sm' | 'md' | 'lg'
  /** Shows a spinner, sets aria-busy, blocks the click. Never fake this. */
  loading?: boolean
  disabled?: boolean
  block?: boolean
  iconLeft?: ReactNode
  iconRight?: ReactNode
  /** Replaces the label while loading, e.g. "Resolving copies…" */
  loadingLabel?: string
  children?: ReactNode
  /** Render as an anchor when the action is navigation. */
  as?: 'button' | 'a'

  /* The three below are the adaptation `as: 'a'` forces. `ButtonHTMLAttributes`
     carries no anchor attributes, and the kits pass all three — Insights opens
     Grafana with `as="a" href="/grafana/" target="_blank" rel="noreferrer noopener"`.
     The element type argument is `HTMLElement` rather than the contract's
     `HTMLButtonElement` for the same reason: a `MouseEventHandler<HTMLButtonElement>`
     cannot be attached to an anchor, so the contract's own polymorphism would not
     compile. */
  /** The anchor's destination. Only read when `as="a"`. */
  href?: string
  target?: HTMLAttributeAnchorTarget
  rel?: string
  ref?: Ref<HTMLElement>
}

/** Primary actions are monochrome on purpose: the teal accent is reserved for links, focus and the
 *  info semantic, so it never competes with a status colour. */
export function Button({
  variant = 'secondary',
  size = 'md',
  loading = false,
  disabled = false,
  block = false,
  iconLeft,
  iconRight,
  loadingLabel,
  children,
  as = 'button',
  href,
  target,
  rel,
  ref,
  className,
  onClick,
  ...rest
}: ButtonProps) {
  const cls = clsx(
    'u-btn',
    `u-btn--${variant}`,
    size !== 'md' && `u-btn--${size}`,
    block && 'u-btn--block',
    className,
  )

  /** Both `disabled` and `loading` refuse the click; only one of them says why. */
  const inert = disabled || loading
  const busy = loading || undefined

  /**
   * One callback for two element types. A `RefObject<HTMLButtonElement>` is not
   * assignable to an anchor's ref slot and vice versa, so the component takes a
   * `Ref<HTMLElement>` and attaches whichever node it actually rendered.
   */
  const attachRef = useCallback(
    (node: HTMLElement | null) => {
      if (typeof ref === 'function') ref(node)
      else if (ref) ref.current = node
    },
    [ref],
  )

  /**
   * patterns.md §1: one pending action, said where it was clicked. `loadingLabel`
   * **replaces** the label rather than appending to it, so the accessible name is
   * the pending sentence and not "Play Finding copies…".
   *
   * The spinner takes the leading icon's slot and the label slot survives, so the
   * control keeps its box while it is busy — it never collapses to a bare spinner
   * and the row it sits in does not reflow.
   */
  const content = (
    <>
      {loading ? <span className="u-btn__spinner" aria-hidden="true" /> : iconLeft}
      <span className={loading ? 'u-btn__pending' : undefined}>
        {loading && loadingLabel ? loadingLabel : children}
      </span>
      {!loading && iconRight}
    </>
  )

  if (as === 'a') {
    /**
     * An anchor cannot be disabled — `disabled` is not an anchor attribute and a
     * disabled link is not a concept. It carries `aria-disabled` instead, and the
     * click is suppressed here because the browser will otherwise navigate.
     */
    const handleClick = (event: ReactMouseEvent<HTMLAnchorElement>) => {
      if (inert) {
        event.preventDefault()
        return
      }
      onClick?.(event)
    }

    return (
      <a
        {...rest}
        className={cls}
        href={href}
        target={target}
        rel={rel}
        aria-disabled={inert || undefined}
        aria-busy={busy}
        onClick={handleClick}
        ref={attachRef}
      >
        {content}
      </a>
    )
  }

  /**
   * **`type="button"` by default, and `{...rest}` first so a caller can still
   * pass `type="submit"`.**
   *
   * HTML's default is `submit`, which for an action primitive is a footgun
   * rather than a convenience: this console's confirm dialogs, review-queue
   * triage and sync triggers all sit inside surfaces that contain a form, and a
   * button that forgot `type="button"` submits it — reloading the page and
   * losing the 202 receipt that was the only record of the queued job. Every
   * call site remembering is not a design; a default that is right for the
   * overwhelming majority is.
   */
  return (
    <button
      type="button"
      {...rest}
      className={cls}
      disabled={inert}
      aria-busy={busy}
      onClick={onClick}
      ref={attachRef}
    >
      {content}
    </button>
  )
}
