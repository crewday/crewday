import { useCallback, useRef, type ReactEventHandler } from "react";

// Native <dialog> modal helpers (§14 Web). A dialog opened with
// `showModal()` gives us a real focus trap, Esc-to-close, and a
// `::backdrop` scrim for free — the behaviour `FormModal` already
// relies on. These helpers keep the open/close plumbing in one place
// so drawers don't each hand-roll a trap.

export function openModalDialog(dialog: HTMLDialogElement): void {
  if (dialog.open) return;
  if (typeof dialog.showModal === "function") {
    try {
      dialog.showModal();
      return;
    } catch {
      // showModal throws if the node is detached or already open;
      // fall back to a non-modal open so the content still renders.
    }
  }
  dialog.setAttribute("open", "");
}

export function closeModalDialog(dialog: HTMLDialogElement): void {
  if (!dialog.open) return;
  if (typeof dialog.close === "function") {
    try {
      dialog.close();
      return;
    } catch {
      // Fall through to removing the attribute if close() is unavailable.
    }
  }
  dialog.removeAttribute("open");
}

// Removing a modal <dialog> from the DOM makes the browser run its own
// "element removed from the top layer" focus fix-up, which resets focus
// to <body> — and that runs after a synchronous `.focus()` here, so it
// would clobber the restore. Deferring past a frame lets our restore win.
function restoreFocus(trigger: HTMLElement): void {
  const apply = (): void => {
    // If a modal dialog still owns focus by the time this frame runs, the
    // element we're restoring from is not actually gone — React 19
    // StrictMode's dev-only detach/reattach keeps the node mounted and
    // open, so restoring here would yank focus out of a live modal. On a
    // real close the node is removed and focus has fallen to <body>, so
    // this guard passes and the restore proceeds.
    const active = document.activeElement;
    if (active instanceof HTMLElement && active.closest("dialog[open]")) return;
    if (trigger.isConnected) trigger.focus({ preventScroll: true });
  };
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(apply);
  else apply();
}

export interface ModalDialogBinding {
  // Ref for the <dialog>: opens it modal on mount and restores focus on
  // unmount (see below).
  ref: (node: HTMLDialogElement | null) => void;
  // Wire onto the dialog's `onCancel`. It preventDefaults the native Esc
  // close so the browser never runs its own close + focus-restore (which
  // races with, and clobbers, React's unmount), then delegates the close
  // to `onDismiss` so React drives the unmount and the ref below is the
  // sole focus authority. When `onDismiss` is omitted the dialog is a
  // mandatory gate: Esc is swallowed and it stays open.
  onCancel: ReactEventHandler<HTMLDialogElement>;
}

// Binds a native <dialog> that mounts when opened and unmounts when
// closed. On attach the ref records the element that had focus (the
// trigger) and opens the dialog as a modal (focus trap + ::backdrop). On
// detach it returns focus to that trigger — a native <dialog> only
// restores focus through its own close algorithm, which a React unmount
// skips, so keyboard/AT users would otherwise be dropped on <body>.
//
// `onDismiss`, when given, is the caller's close handler. It wires both
// Esc (via `onCancel`) and backdrop click-to-close: a click whose target
// is the dialog element itself landed on the ::backdrop rather than the
// panel content. Backdrop dismissal is a native listener (not a JSX
// handler) so it doesn't read as an interactive handler on a
// non-interactive `<dialog>`.
export function useModalDialog(onDismiss?: () => void): ModalDialogBinding {
  const triggerRef = useRef<HTMLElement | null>(null);
  const dismissRef = useRef<(() => void) | undefined>(onDismiss);
  dismissRef.current = onDismiss;
  const detachRef = useRef<(() => void) | null>(null);
  const ref = useCallback((node: HTMLDialogElement | null) => {
    detachRef.current?.();
    detachRef.current = null;
    if (node) {
      // Record the trigger to restore focus to on close. Guard against
      // React StrictMode's dev-only attach/detach/re-attach cycle: on the
      // re-attach, focus has already moved inside the modal, so only
      // capture when focus is still outside any open dialog. We never
      // clear the captured trigger, so the re-attach keeps the real one.
      const active = document.activeElement;
      if (active instanceof HTMLElement && !active.closest("dialog[open]")) {
        triggerRef.current = active;
      }
      openModalDialog(node);
      const onBackdropClick = (event: MouseEvent): void => {
        if (event.target === node) dismissRef.current?.();
      };
      node.addEventListener("click", onBackdropClick);
      detachRef.current = () => node.removeEventListener("click", onBackdropClick);
      return;
    }
    const trigger = triggerRef.current;
    if (trigger && trigger.isConnected) restoreFocus(trigger);
  }, []);
  const onCancel = useCallback<ReactEventHandler<HTMLDialogElement>>((event) => {
    event.preventDefault();
    dismissRef.current?.();
  }, []);
  return { ref, onCancel };
}
