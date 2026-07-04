// Tracks task occurrences the current tab's user just completed, so the
// SSE bridge can tell a *supersession* (§06 last-write-wins: another
// user re-completed the same occurrence) apart from the echo of the
// user's own completion frame.
//
// The `/complete` response always stamps `completed_by_user_id` with the
// current caller (`app/domain/tasks/completion.py` — the later racer
// wins), so the client can never learn another user's name from
// `onSuccess`. The "Completed by <name>" signal is inherently SSE-driven
// (a `task.completed` frame whose `completed_by` differs from who this
// tab recorded). `TodayPage` records here on completion; `@/lib/sse`
// consumes here when a frame lands.

interface SelfCompletion {
  byUserId: string | null;
  at: number;
}

const TTL_MS = 5 * 60_000;

const store = new Map<string, SelfCompletion>();

function prune(now: number): void {
  for (const [taskId, entry] of store) {
    if (now - entry.at > TTL_MS) store.delete(taskId);
  }
}

/**
 * Record that the current user just completed `taskId`. `byUserId` is
 * the `completed_by_user_id` the server echoed back (the current
 * caller), used later to distinguish an own-completion frame from a
 * supersession by a different user.
 */
export function recordSelfCompletion(taskId: string, byUserId: string | null): void {
  const now = Date.now();
  prune(now);
  store.set(taskId, { byUserId, at: now });
}

/**
 * Return `true` iff the current user recently completed `taskId` and the
 * incoming `completedBy` is a *different, known* user — i.e. someone
 * superseded this tab's completion. On a supersession the entry is
 * evicted so a duplicate frame can't re-toast. An own-completion echo
 * (`completedBy === recorded byUserId`) returns `false` and keeps the
 * entry, since a later racer could still supersede it.
 */
export function takeSupersededCompletion(taskId: string, completedBy: string): boolean {
  prune(Date.now());
  const entry = store.get(taskId);
  if (!entry) return false;
  if (entry.byUserId === null || entry.byUserId === completedBy) return false;
  store.delete(taskId);
  return true;
}

/** Test hook — drop all tracked completions. */
export function __resetRecentCompletionsForTests(): void {
  store.clear();
}
