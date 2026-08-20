import { useNavigate } from 'react-router-dom'
import { Icon, STATE_ICON, Toast, ToastStack as Stack, TextLink } from '@/design-system'
import { useToasts, type Toast as QueuedToast } from '@/patterns'
import { CONSOLE_BASE } from '@/api/paths'

/**
 * Binds the toast queue to the design system's `Toast`.
 *
 * The design system knows nothing about routing or about Usher's 202 shape;
 * this is where the two meet, which is the whole point of the `features/`
 * boundary. Two rules survive the crossing:
 *
 * · **The word is "Queued."** Never "Done", never "Saved", never a bare
 *   checkmark. The receipt's own title carries it.
 * · **The key is the only record.** Every mutating admin action answers
 *   `202 {kind, key}` and no route can look that key up, so a receipt has no
 *   timer and the key is selectable — an operator pastes it into a log search.
 */
export function ToastStack() {
  const { toasts, dismiss } = useToasts()
  if (toasts.length === 0) return null
  return (
    <Stack>
      {toasts.map((toast) => (
        <QueuedToastView key={toast.id} toast={toast} onDismiss={() => dismiss(toast.id)} />
      ))}
    </Stack>
  )
}

function QueuedToastView({ toast, onDismiss }: { toast: QueuedToast; onDismiss: () => void }) {
  const navigate = useNavigate()

  if (toast.variant === 'receipt') {
    return (
      <Toast
        tone="info"
        title={toast.title}
        jobKey={toast.jobKey}
        {...(toast.coalesced === undefined ? {} : { coalesced: toast.coalesced })}
        onDismiss={onDismiss}
        {...(toast.destination
          ? {
              action: (
                // The destination is honest even when the surface it points at
                // is itself REQUIRES BACKEND WORK — the pointer says where to
                // look, and Pipeline says why it cannot show you yet.
                //
                // A real `href` with an intercepted click, rather than a router
                // `Link`: `TextLink` is a design-system component and the
                // library may not import `react-router-dom`. Middle-click and
                // "open in new tab" still work, which they would not if this
                // were a button.
                <TextLink
                  href={`${CONSOLE_BASE}${toast.destination.to}`}
                  onClick={(event) => {
                    if (event.metaKey || event.ctrlKey || event.shiftKey) return
                    event.preventDefault()
                    navigate(destinationOf(toast))
                  }}
                >
                  {toast.destination.label}
                </TextLink>
              ),
            }
          : {})}
      >
        {toast.detail}
      </Toast>
    )
  }
  return (
    <Toast
      tone={toast.tone}
      title={toast.title}
      icon={<Icon name={STATE_ICON[toast.tone]} size={16} />}
      onDismiss={onDismiss}
    >
      {toast.detail}
    </Toast>
  )
}

/** Narrowed away from `| undefined` for the click handler's closure. */
function destinationOf(toast: QueuedToast): string {
  return toast.variant === 'receipt' && toast.destination ? toast.destination.to : '/'
}
