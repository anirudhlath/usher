/**
 * The design system's public surface — 28 components in ten groups.
 *
 * Screens import from here. **Nothing under `design-system/` may import from
 * `api/`, `features/` or `app/`**: components take data as props, which is what
 * makes the library reusable, keeps its tests free of MSW, and stops an API
 * shape leaking into a visual contract. `features/` is where an Usher DTO is
 * mapped onto these props.
 */
export * from './components/icon'
export * from './components/actions'
export * from './components/forms'
export * from './components/navigation'
export * from './components/media'
export * from './components/data'
export * from './components/status'
export * from './components/feedback'
export * from './components/playback'
export * from './components/charts'
