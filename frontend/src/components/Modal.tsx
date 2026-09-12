/**
 * A dialog that behaves like one.
 *
 * Focus is trapped, Escape and a backdrop click close it, `aria-modal` and
 * `aria-labelledby` are set, and focus returns to whatever opened it. None of that is
 * optional: a div with a dark background behind it is not a dialog to a screen reader
 * or to anyone driving the page from the keyboard, and Tab silently walking out of an
 * open modal into the page underneath is the specific failure this prevents.
 *
 * Kept as one component rather than a dependency — this is ~80 lines against a library
 * whose remaining surface (portals, transitions, nested dialogs) nothing here needs.
 */

import { useCallback, useEffect, useId, useRef, type ReactNode } from 'react'

/** Everything focusable, minus anything explicitly removed from the tab order. */
const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

export function Modal({
  open,
  title,
  description,
  onClose,
  children,
  footer,
}: {
  open: boolean
  title: string
  description?: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
}) {
  const panel = useRef<HTMLDivElement>(null)
  const returnFocusTo = useRef<HTMLElement | null>(null)
  const titleId = useId()
  const descriptionId = useId()

  const focusables = useCallback((): HTMLElement[] => {
    if (panel.current === null) return []
    return Array.from(panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
      (element) => element.offsetParent !== null,
    )
  }, [])

  useEffect(() => {
    if (!open) return

    returnFocusTo.current = document.activeElement as HTMLElement | null
    // Focus the first control rather than the panel, so a keyboard user lands on
    // something they can act on instead of having to Tab into the dialog.
    const first = focusables()[0] ?? panel.current
    first?.focus()

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose()
        return
      }
      if (event.key !== 'Tab') return

      const items = focusables()
      if (items.length === 0) {
        event.preventDefault()
        return
      }
      const first = items.at(0)
      const last = items.at(-1)
      if (first === undefined || last === undefined) return
      // Wrap at both ends. Without this Tab leaves the dialog for the page behind it,
      // which is still rendered and still focusable.
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    // The page behind must not scroll under an open dialog.
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      document.body.style.overflow = previousOverflow
      returnFocusTo.current?.focus()
    }
  }, [open, onClose, focusables])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-ink/80 px-4 py-10 sm:py-16"
      // The backdrop closes, but only when the backdrop itself is the target — a
      // mousedown that started inside the panel and drifted out must not close it.
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description === undefined ? undefined : descriptionId}
        tabIndex={-1}
        className="w-full max-w-2xl rounded-lg border border-line bg-ink-2 focus:outline-none"
      >
        <div className="flex items-start justify-between gap-6 border-b border-line px-6 py-5 sm:px-8">
          <div>
            <h2
              id={titleId}
              className="font-display text-2xl font-normal tracking-tight text-cream"
            >
              {title}
            </h2>
            {description !== undefined && (
              <p
                id={descriptionId}
                className="mt-2 max-w-lg text-sm font-light leading-relaxed text-ash"
              >
                {description}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close dialog"
            className="-mr-2 -mt-1 rounded-md px-2 py-1 text-xl leading-none text-slate transition-colors duration-300 ease-refined hover:text-cream"
          >
            &times;
          </button>
        </div>

        <div className="px-6 py-6 sm:px-8">{children}</div>

        {footer !== undefined && (
          <div className="border-t border-line px-6 py-5 sm:px-8">{footer}</div>
        )}
      </div>
    </div>
  )
}
