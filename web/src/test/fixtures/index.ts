/**
 * Every fixture, in one import.
 *
 * These are **plain typed data derived from the real DTO shapes in
 * `schema.d.ts`**, not invented objects. Where the generated type is optimistic
 * — `response_model_exclude_unset=True` makes three routes send fewer keys than
 * the schema declares — the fixture is typed with `Omit` so the absence is
 * checked by the compiler rather than described in a comment. See
 * `titles.ts`, `browse.ts` and `people.ts`.
 *
 * `handlers.ts` composes them into MSW handlers; a test that wants a different
 * answer overrides one route with `server.use(...)` rather than editing these.
 */

export * as ids from './ids'
export * from './home'
export * from './browse'
export * from './search'
export * from './titles'
export * from './people'
export * from './admin'
export * from './meta'
export * from './play'
export * from './problems'
