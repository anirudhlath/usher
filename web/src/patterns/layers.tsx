import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

/**
 * `Esc` closes exactly one layer, innermost first (patterns.md §9).
 *
 * **One document-level listener, not one per layer.** Every component handling
 * its own `Esc` produces the failure this exists to prevent: a combobox open
 * inside a dialog inside the developer drawer takes one `Esc` and closes all
 * three, because all three listeners fire on the same event and
 * `stopPropagation` on a document listener does not order them. A single
 * listener over an ordered stack closes the top one and nothing else.
 *
 * The order is registration order, which is mount order, which is nesting
 * order — a listbox inside a popover mounts after it. `kind` is carried for
 * debugging and for the one place order is *not* nesting: the developer drawer
 * sits above modals on purpose (`--z-devdrawer` 700 against `--z-modal` 410),
 * because you have to be able to read the request journal for the failed call
 * that put the modal on screen.
 */
export type LayerKind = 'listbox' | 'popover' | 'dialog' | 'drawer' | 'sheet'

interface Layer {
  id: string
  kind: LayerKind
  close: () => void
}

interface LayerStack {
  push: (layer: Layer) => void
  pop: (id: string) => void
  /** Topmost first. Read by tests and by the dev drawer's own diagnostics. */
  open: () => ReadonlyArray<Pick<Layer, 'id' | 'kind'>>
}

const LayerStackContext = createContext<LayerStack | null>(null)

export function LayerStackProvider({ children }: { children: ReactNode }) {
  const layers = useRef<Layer[]>([])
  const [, force] = useState(0)

  const push = useCallback((layer: Layer) => {
    layers.current = [...layers.current.filter((one) => one.id !== layer.id), layer]
    force((n) => n + 1)
  }, [])

  const pop = useCallback((id: string) => {
    layers.current = layers.current.filter((one) => one.id !== id)
    force((n) => n + 1)
  }, [])

  const open = useCallback(() => layers.current.map(({ id, kind }) => ({ id, kind })).reverse(), [])

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape') return
      const top = layers.current.at(-1)
      if (!top) return
      // Not `stopPropagation`: the event has already reached everything by the
      // time a document listener sees it. `preventDefault` is what stops the
      // browser's own Escape behaviour (cancelling an in-flight navigation,
      // exiting fullscreen) from firing as well as this close.
      event.preventDefault()
      top.close()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [])

  const value = useMemo<LayerStack>(() => ({ push, pop, open }), [push, pop, open])
  return <LayerStackContext.Provider value={value}>{children}</LayerStackContext.Provider>
}

/**
 * Registers a layer while it is open. `onClose` must be stable or memoised —
 * it is read from a ref, so a changing identity does not re-register, but a
 * stale closure would close the wrong state.
 */
export function useLayer(kind: LayerKind, isOpen: boolean, onClose: () => void): void {
  const stack = useContext(LayerStackContext)
  const id = useId()
  const close = useRef(onClose)

  // **Assigned in an effect, not during render.** A render may be started and
  // thrown away under concurrent rendering, so writing a ref in the render body
  // is a side effect on a path React does not promise to run exactly once.
  // Declared before the registration effect below so the ref is current by the
  // time anything can read it — and nothing can read it before then anyway,
  // since `close` is only ever called from a keydown.
  useEffect(() => {
    close.current = onClose
  })

  useEffect(() => {
    if (!stack || !isOpen) return
    stack.push({ id, kind, close: () => close.current() })
    return () => stack.pop(id)
  }, [stack, isOpen, id, kind])
}

/** Topmost first. Empty outside a provider, which is the component-test case. */
export function useOpenLayers(): ReadonlyArray<Pick<Layer, 'id' | 'kind'>> {
  const stack = useContext(LayerStackContext)
  return stack ? stack.open() : []
}
