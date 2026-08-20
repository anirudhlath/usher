import type { ReactNode } from 'react'
import clsx from 'clsx'
import { Icon, STATE_ICON } from '../icon'

/**
 * The one badge. Tones carry semantics, `tier` carries enrichment_state, `mono` carries composed
 * technical facts ("2160p · HDR10 · HEVC · MKV" — there is no quality string in the API, you compose it).
 * Colour is never the only carrier: pass an icon or make the word itself the signal.
 *
 */
export interface BadgeProps {
  tone?: 'neutral' | 'good' | 'warn' | 'bad' | 'info'
  /** Overrides tone with an enrichment tier treatment. */
  tier?: 'skeleton' | 'stub' | 'enriched' | 'failed'
  mono?: boolean
  outline?: boolean
  icon?: ReactNode
  children?: ReactNode
}

export type BadgeTone = NonNullable<BadgeProps['tone']>
export type BadgeTier = NonNullable<BadgeProps['tier']>

/**
 * patterns.md §12 forbids colour-only encoding: every state is hue **+ icon + word**.
 * A caller who passes `tone="warn"` and forgets the glyph would be encoding in hue
 * alone, so the fixed state glyph is supplied rather than merely allowed. `neutral`
 * has no state to encode, and the four tiers carry their own word
 * (skeleton / stub / enriched / failed), so neither gets one.
 */
function defaultGlyph(tone: BadgeTone, tier: BadgeTier | undefined): ReactNode {
  if (tier || tone === 'neutral') return undefined
  return <Icon name={STATE_ICON[tone]} />
}

/** The single badge — semantic tones, enrichment tiers, and mono technical facts. */
export function Badge({ tone = 'neutral', tier, mono = false, outline = false, icon, children }: BadgeProps) {
  return (
    <span
      className={clsx(
        'u-badge',
        tier ? `u-badge--${tier}` : `u-badge--${tone}`,
        mono && 'u-badge--mono',
        outline && 'u-badge--outline',
      )}
    >
      {icon ?? defaultGlyph(tone, tier)}
      {children}
    </span>
  )
}
