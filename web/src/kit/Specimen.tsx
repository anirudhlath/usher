import type { CSSProperties, ReactNode } from 'react'
import clsx from 'clsx'

/**
 * The two frames every specimen sheet is built from.
 *
 * They exist so that a Playwright spec can address one rendering rather than the
 * whole page: `#group-actions` is a section, `[data-specimen="Button/loading"]`
 * is one component in one state. Both attributes are part of the contract with
 * `e2e/kit.spec.ts` — renaming one renames a screenshot baseline.
 */
export interface SpecimenProps {
  /** `Component/state`. Stable: it is how a spec addresses this rendering. */
  name: string
  /** One sentence, for a state whose point is not visible in the rendering. */
  note?: string
  /**
   * For a component that paints outside the flow. `.u-scrim` and `.u-toasts` are
   * `position: fixed`; paint containment makes the stage their containing block
   * so an open dialog stays in its own box instead of covering the gallery.
   */
  overlay?: boolean
  /** Caps the stage, for things that would otherwise fill the row. */
  width?: number
  /** Reserves room for a popup that escapes the flow — the combobox listbox. */
  minHeight?: number
  /** Takes the whole row. Tables and wide sheets. */
  wide?: boolean
  children: ReactNode
}

export function Specimen({
  name,
  note,
  overlay = false,
  width,
  minHeight,
  wide = false,
  children,
}: SpecimenProps) {
  /* Built rather than spread so `exactOptionalPropertyTypes` has nothing to
     object to: an absent width must not become `maxWidth: undefined`. */
  const style: CSSProperties = {}
  if (width !== undefined) style.maxWidth = width
  if (minHeight !== undefined) style.minHeight = minHeight

  return (
    <div className={clsx('k-spec', wide && 'k-spec--wide')} data-specimen={name}>
      <span className="k-spec__label">{name}</span>
      <div className={clsx('k-spec__stage', overlay && 'k-spec__stage--overlay')} style={style}>
        {children}
      </div>
      {note ? <p className="k-spec__note">{note}</p> : null}
    </div>
  )
}

export interface GroupSectionProps {
  /** The group's directory name — `actions`, `forms`. Becomes `#group-actions`. */
  id: string
  title: string
  blurb: string
  children: ReactNode
}

export function GroupSection({ id, title, blurb, children }: GroupSectionProps) {
  const headingId = `group-${id}-heading`
  return (
    <section id={`group-${id}`} className="k-group" aria-labelledby={headingId}>
      <div className="k-group__head">
        <h2 className="k-group__title" id={headingId}>
          {title}
        </h2>
        <p className="k-group__blurb">{blurb}</p>
      </div>
      <div className="k-group__grid">{children}</div>
    </section>
  )
}
