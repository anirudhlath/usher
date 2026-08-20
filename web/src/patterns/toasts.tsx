import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'

/**
 * The toast queue (patterns.md §3 and §6).
 *
 * Two kinds, and the difference is not cosmetic:
 *
 * · **A receipt** — the answer to a 202. Every mutating admin action returns
 *   `202 {kind, key}` and *there is no route to look that key up*, so the toast
 *   is the only record the operator gets. It **persists until dismissed**.
 * · **A notice** — a failure the user did not trigger on this screen, or a
 *   confirmation of something reversible. It auto-dismisses.
 *
 * `aria-live="polite"`, never `assertive`. Nothing in this product is
 * assertive, including a failed sync.
 */
export interface ToastReceipt {
  id: string
  variant: 'receipt'
  /** "Queued a full sync of Living Room". The word is *Queued*. */
  title: string
  /** What it will do, and how long it took last time. */
  detail?: string
  /** The `202` body's `kind:key`, printed in mono and selectable. */
  jobKey: string
  /** Stated when known: "It coalesced with a job already running." */
  coalesced?: boolean
  /** Where evidence will appear — even when that surface is BackendWork. */
  destination?: { label: string; to: string }
}

export interface ToastNotice {
  id: string
  variant: 'notice'
  tone: 'good' | 'warn' | 'bad' | 'info'
  title: string
  detail?: string
}

export type Toast = ToastReceipt | ToastNotice

interface ToastQueue {
  toasts: readonly Toast[]
  receipt: (toast: Omit<ToastReceipt, 'id' | 'variant'>) => void
  notice: (toast: Omit<ToastNotice, 'id' | 'variant'>) => void
  dismiss: (id: string) => void
}

const ToastContext = createContext<ToastQueue | null>(null)

/** Long enough to read two lines; §12's motion budget governs the animation, not this. */
const NOTICE_MS = 6_000

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<readonly Toast[]>([])
  const nextId = useRef(0)
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>())

  const dismiss = useCallback((id: string) => {
    const timer = timers.current.get(id)
    if (timer) {
      clearTimeout(timer)
      timers.current.delete(id)
    }
    setToasts((current) => current.filter((one) => one.id !== id))
  }, [])

  const receipt = useCallback((toast: Omit<ToastReceipt, 'id' | 'variant'>) => {
    const id = `toast-${nextId.current++}`
    // No timer. A receipt carries a key nothing can query, so dismissing it on
    // the operator's behalf destroys the only copy.
    setToasts((current) => [...current, { ...toast, id, variant: 'receipt' }])
  }, [])

  const notice = useCallback(
    (toast: Omit<ToastNotice, 'id' | 'variant'>) => {
      const id = `toast-${nextId.current++}`
      setToasts((current) => [...current, { ...toast, id, variant: 'notice' }])
      timers.current.set(
        id,
        setTimeout(() => dismiss(id), NOTICE_MS),
      )
    },
    [dismiss],
  )

  const value = useMemo<ToastQueue>(
    () => ({ toasts, receipt, notice, dismiss }),
    [toasts, receipt, notice, dismiss],
  )
  return <ToastContext.Provider value={value}>{children}</ToastContext.Provider>
}

export function useToasts(): ToastQueue {
  const queue = useContext(ToastContext)
  if (!queue) throw new Error('useToasts requires a ToastProvider')
  return queue
}
