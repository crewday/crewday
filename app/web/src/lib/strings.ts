/**
 * Tiny string helpers shared across pages. Each one was copy-pasted
 * into ~5 components; collecting them here keeps the behaviour (and
 * locale quirks) in one place.
 */

export function cap(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}
