import type { AnchorHTMLAttributes, ReactNode, Ref } from 'react'
import clsx from 'clsx'
import { Icon } from '../icon'

/** Inline link. Teal, underlined at 38% opacity, full-opacity underline on hover. */
export interface TextLinkProps extends AnchorHTMLAttributes<HTMLAnchorElement> {
  /** Neutral until hover — for links inside dense tables where teal would be noise. */
  quiet?: boolean
  /** Adds target/rel and a visually-hidden "(opens in a new tab)". Use for Grafana and TMDb. */
  external?: boolean
  children?: ReactNode
  ref?: Ref<HTMLAnchorElement>
}

/**
 * A real anchor, and nothing more. The contract has no `as`/`href` split, so this
 * component is not polymorphic and knows nothing about routing — `design-system/`
 * may not import `react-router-dom`. A `features/` screen navigates the way the
 * kits do, with `href` plus an `onClick` that calls the router.
 *
 * `external` carries three things, because one of them alone is a lie: `rel` so the
 * new document cannot reach `window.opener`, the `external-link` glyph so the
 * destination is visible before the click, and the visually-hidden sentence so it
 * is audible too. The glyph is `aria-hidden` — the sentence is the announcement.
 * `target`/`rel` are applied after `...rest` so a consumer cannot drop `noopener`
 * from an external link by passing a `rel` of their own, and are not applied at all
 * on an internal one, which would otherwise clobber a `target` the consumer meant.
 */
export function TextLink({
  quiet = false,
  external = false,
  children,
  className,
  ref,
  ...rest
}: TextLinkProps) {
  return (
    <a
      ref={ref}
      {...rest}
      {...(external ? { target: '_blank', rel: 'noreferrer noopener' } : {})}
      className={clsx('u-link', quiet && 'u-link--quiet', className)}
    >
      {children}
      {external && <Icon name="external-link" />}
      {external && <span className="u-visually-hidden"> (opens in a new tab)</span>}
    </a>
  )
}
