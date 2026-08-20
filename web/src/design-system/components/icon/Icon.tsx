import type { CSSProperties, ReactNode } from 'react'
import clsx from 'clsx'
import { ICONS, type IconName } from './registry'

export type IconSize = 16 | 20 | 24

/**
 * Lucide icon wrapper. 16 inline with text and in compact rows, 20 in controls and nav, 24 in
 * empty states and headers — no other sizes. Stroke 1.75 at 16/20, 2 at 24. Always currentColor.
 *
 * State icons are fixed so colour is never the only carrier:
 * check-circle (good) · alert-triangle (warn) · x-circle (bad) · info (info) ·
 * circle-dashed (never computed) · history (stale).
 */
export interface IconProps {
  /** A name from the registry. Type-checked — an unknown name will not compile. */
  name?: IconName
  size?: IconSize
  /** A pre-imported SVG element, for a glyph outside the registry. Wins over `name`. */
  svg?: ReactNode
  /** Only when the icon is the sole carrier of meaning; otherwise it stays aria-hidden. */
  label?: string
  className?: string
  style?: CSSProperties
}

/**
 * The stroke width lives in `icon.css` keyed off the size class, so the Lucide
 * component's own `strokeWidth` prop is left alone — otherwise the two disagree
 * and whichever loses the cascade is a silent visual regression.
 */
export function Icon({ name, size = 16, svg, label, className, style }: IconProps) {
  const Glyph = name ? ICONS[name] : undefined
  return (
    <span
      className={clsx('u-icon', `u-icon--${size}`, !svg && !Glyph && 'u-icon--fallback', className)}
      style={style}
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      data-icon={name}
    >
      {svg ?? (Glyph ? <Glyph absoluteStrokeWidth={false} /> : null)}
    </span>
  )
}
