import { useEffect, useRef } from "react";

// Universal Escape-to-close for scrim-backed drawers across the app
// (§14 Web). Native <dialog> already handles Escape via the browser;
// this hook exists for the custom aside + scrim drawers that don't.
//
// Active drawers are tracked as a stack so Escape only closes the
// most-recently-mounted drawer.
//
// `active` lets the caller gate the listener (e.g. only when the
// drawer is actually rendered), but defaults to true so the common
// case "mount = active" stays a one-liner.

interface CloseOnEscapeEntry {
  close: () => void;
}

const closeOnEscapeStack: CloseOnEscapeEntry[] = [];

function removeEntry(entry: CloseOnEscapeEntry): void {
  const index = closeOnEscapeStack.indexOf(entry);
  if (index >= 0) closeOnEscapeStack.splice(index, 1);
}

export function useCloseOnEscape(
  onClose: () => void,
  active: boolean = true,
): void {
  const entryRef = useRef<CloseOnEscapeEntry>({ close: onClose });
  entryRef.current.close = onClose;

  useEffect(() => {
    if (!active) return;
    const entry = entryRef.current;
    closeOnEscapeStack.push(entry);
    function handler(ev: KeyboardEvent): void {
      if (ev.key !== "Escape" && ev.key !== "Esc") return;
      // If the event originated inside a native <dialog>, let the
      // browser close that instead of intercepting here.
      const target = ev.target;
      if (target instanceof Element && target.closest("dialog[open]")) return;
      if (closeOnEscapeStack[closeOnEscapeStack.length - 1] !== entry) return;
      ev.stopPropagation();
      ev.stopImmediatePropagation();
      entry.close();
    }
    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("keydown", handler);
      removeEntry(entry);
    };
  }, [active]);
}
