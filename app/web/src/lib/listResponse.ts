// crewday — generic cursor envelope helpers.
//
// Every `/api/v1/` collection endpoint that uses `app.api.pagination`
// returns the same shape (spec §12 "Pagination"):
//
//     { data: T[], next_cursor: string | null, has_more: boolean }
//
// This module owns the canonical TypeScript projection so callers
// stop redeclaring it inline. `unwrapList` peels the envelope when a
// page only needs `data[]`; `ListEnvelope<T>` is the full shape for
// callers that follow the cursor (load-more, paginated tables).
//
// Sister type to :class:`app.api.pagination.PageResult` on the server.

/**
 * Wire shape of every cursor-paginated `/api/v1/` collection endpoint
 * (spec §12). Generic over the row type so each call-site keeps its
 * `T` inferred end-to-end without redeclaring the envelope.
 */
export interface ListEnvelope<T> {
  data: T[];
  next_cursor: string | null;
  has_more: boolean;
}

/**
 * Drop the envelope and return the row array. Use when the caller
 * does not paginate beyond the first page (e.g. small lookup tables
 * the desk-page renders in full); reach for the full envelope shape
 * when `has_more` matters.
 */
export function unwrapList<T>(envelope: ListEnvelope<T>): T[] {
  return envelope.data;
}
