import type { ButtonHTMLAttributes, ReactNode, Ref } from 'react'
import clsx from 'clsx'

/** Icon-only control. `label` is not optional — the reference client shipped unnamed icon buttons
 *  and that was one of the measured accessibility failures. */
export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'aria-label'> {
  /** Accessible name AND tooltip text. Same words in both. */
  label: string
  icon: ReactNode
  size?: 'sm' | 'md'
  /** Adds a --border-control outline so it reads as a control on artwork. */
  outlined?: boolean
  /** Forces 44×44 for touch surfaces. */
  touch?: boolean
  disabled?: boolean
  ref?: Ref<HTMLButtonElement>
}

/**
 * Icon-only control. `label` is required and becomes both the accessible name and the tooltip.
 *
 * `aria-label` is `Omit`ted from the inherited attributes deliberately: patterns.md §12 says this
 * component cannot be constructed without a name, and a consumer who could pass `aria-label` through
 * `...rest` could still overwrite the one `label` gives it — the tooltip and the accessible name
 * would then be different words.
 */
export function IconButton({
  label,
  icon,
  size = 'md',
  outlined = false,
  touch = false,
  disabled,
  className,
  ref,
  ...rest
}: IconButtonProps) {
  const cls = clsx(
    'u-iconbtn',
    outlined && 'u-iconbtn--outlined',
    size === 'sm' && 'u-iconbtn--sm',
    // patterns.md §10: touch overrides density. `--target-touch` is a fixed 44 px
    // and the rule lands after `--sm`, so a compact operator surface with a touch
    // control still has a 44 px target. Nothing here reads the density attribute.
    touch && 'u-iconbtn--touch',
    className,
  )
  /*
   * `aria-label` and `title` come after the spread on purpose. `title` is an
   * ordinary HTML attribute a consumer can pass, and the accessible name and the
   * tooltip are required to be the same words — neither is negotiable from
   * outside.
   */
  return (
    <button
      type="button"
      disabled={disabled}
      ref={ref}
      {...rest}
      className={cls}
      aria-label={label}
      title={label}
    >
      {icon}
    </button>
  )
}
