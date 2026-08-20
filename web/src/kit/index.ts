/**
 * The component gallery's public surface.
 *
 * `export { default }` is what lets `App.tsx` write `lazy(() => import('@/kit'))`
 * and still import through the barrel rather than through the file. The ten
 * sections are named exports so a screen or a spec can mount one group on its
 * own.
 */
export { default, default as Gallery } from './Gallery'
export { GroupSection, Specimen, type GroupSectionProps, type SpecimenProps } from './Specimen'
export { IconSpecimens } from './specimens/icon'
export { ActionsSpecimens } from './specimens/actions'
export { FormsSpecimens } from './specimens/forms'
export { NavigationSpecimens } from './specimens/navigation'
export { MediaSpecimens } from './specimens/media'
export { DataSpecimens } from './specimens/data'
export { StatusSpecimens } from './specimens/status'
export { FeedbackSpecimens } from './specimens/feedback'
export { PlaybackSpecimens } from './specimens/playback'
export { ChartsSpecimens } from './specimens/charts'
