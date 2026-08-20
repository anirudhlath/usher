import clsx from 'clsx'

/**
 * The brand mark. **This wordmark IS the brand mark; there is no logo file and
 * none is pending** — the handoff is explicit that Usher never had one, so this
 * is not a placeholder waiting on an asset.
 *
 * The terminal dot is the only teal in it, and it is the only place in the
 * product where teal appears as identity rather than as link, focus, selection
 * or `info`. The colour step between themes lives in `shell.css`.
 */
export function Wordmark({
  size = 'lg',
  abbreviated = false,
  className,
}: {
  size?: 'lg' | 'sm' | 'sidebar'
  /** The collapsed operator rail has 56 px, which fits `u.` and not `usher.` */
  abbreviated?: boolean
  className?: string
}) {
  return (
    <span className={clsx('u-wordmark', `u-wordmark--${size}`, className)}>
      {abbreviated ? 'u' : 'usher'}
      <span className="u-wordmark__dot" aria-hidden="true">
        .
      </span>
      {/* The dot is decorative punctuation; without this the accessible name of
          a header link reads "usher ." with a stray full stop in the middle of
          a sentence a screen reader is composing. */}
      <span className="u-visually-hidden">{abbreviated ? 'Usher' : ''}</span>
    </span>
  )
}
