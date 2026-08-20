/* `<progress>` is the tag the linter would prefer, and it cannot express this design: a 3 px track
   whose fill colour is `--progress-fill` until `played` flips it to `--progress-played`, sitting
   inside the card button on the artwork's bottom edge. The handoff's contract is `role="progressbar"`
   with `aria-valuetext` in words, and that is what patterns.md §12 requires here. */
// oxlint-disable jsx-a11y/prefer-tag-over-role
import clsx from 'clsx'

/** 3 px watch-progress bar, monochrome, sitting on the bottom edge of artwork. Green when played.
 *  Without `runtimeSeconds` there is no denominator, so it reports "Progress unknown" rather than
 *  guessing a percentage. */
export interface ProgressBarProps {
  positionSeconds?: number
  /** Missing runtime is a real case — the bar then carries no fill and says so. */
  runtimeSeconds?: number | null
  played?: boolean
  label?: string
}

/**
 * patterns.md §12: `role="progressbar"` with `aria-valuetext` in words. Where there is no
 * denominator `aria-valuenow` is **omitted** and `aria-valuetext` says so — a `valuenow` of 0
 * against a `valuemax` of 100 would be a fabricated denominator (§14), announced as "0 percent"
 * by every screen reader, which is a different claim from "we do not know".
 *
 * Nothing here animates. The fill's width is set outright: `media.css` declares no transition on
 * it, and a width that eases is a layout animation on a bar whose entire job is to report state
 * (the 180 ms motion budget).
 */
export function ProgressBar({
  positionSeconds = 0,
  runtimeSeconds,
  played = false,
  label,
}: ProgressBarProps) {
  /** 0 is not a runtime. Only a positive number is a denominator we can divide by. */
  const denominated = typeof runtimeSeconds === 'number' && runtimeSeconds > 0
  const pct = played
    ? 100
    : denominated
      ? Math.min(100, Math.max(0, (positionSeconds / runtimeSeconds) * 100))
      : 0

  const text =
    label ??
    (played
      ? 'Watched'
      : denominated
        ? `${Math.round(positionSeconds / 60)} of ${Math.round(runtimeSeconds / 60)} min watched`
        : 'Progress unknown — no runtime on record')

  return (
    <div
      className={clsx('u-progress', played && 'u-progress--played')}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={denominated ? Math.round(pct) : undefined}
      aria-valuetext={text}
      aria-label={text}
    >
      <div className="u-progress__fill" style={{ width: `${pct}%` }} />
    </div>
  )
}
