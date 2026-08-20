/**
 * The API layer's public surface.
 *
 * Import through this barrel, never through the files. Two rules ride on that:
 * `hooks.ts` is the only thing that issues a request, so a feature reaching for
 * `request` directly is visible in a diff; and `schema.d.ts` is generated and
 * has no business being imported anywhere — the types it produces are
 * re-exported from `hooks.ts` under names that say what they are.
 */

export {
  API_BASE,
  IMAGE_WIDTHS,
  PLAYBACK_TICKET_PLACEHOLDER,
  REDACTED_KEYS,
  UsherProblem,
  imageUrl,
  loadOperationTemplates,
  redact,
  request,
  streamPath,
  ticketOf,
  type Ok,
  type OkPost,
  type OkPut,
  type QueryValue,
  type RequestOptions,
  type Schemas,
} from './client'

export {
  RECOVERY,
  TRACE_HEADER,
  fieldErrors,
  isProblemCode,
  parseRetryAfter,
  parseTraceResponse,
  recoveryFor,
  type FieldError,
  type ProblemCode,
  type ProblemDocument,
  type ProblemRecovery,
  type ProblemScale,
  type RecoveryEntry,
} from './problem'

export {
  EVENT_NAMES,
  eventStreamUrl,
  openEventStream,
  parseFrame,
  useEventStream,
  type BootstrapProgress,
  type ConnectionState,
  type EventName,
  type EventPayloads,
  type EventStream,
  type EventStreamOptions,
  type EventStreamStatus,
  type ResyncRequired,
  type RowInvalidated,
  type SyncProgress,
  type TitleUpdated,
  type UseEventStreamOptions,
  type UsherEvent,
  type WatchStateUpdated,
} from './events'

export {
  clear as clearJournal,
  exercised,
  getEntries,
  matchTemplate,
  record,
  setTemplates,
  subscribe as subscribeToJournal,
  useJournal,
  type LogEntry,
} from './devlog'

export { CONSOLE_BASE, USHER_API_ROOTS } from './paths'

export * from './hooks'
