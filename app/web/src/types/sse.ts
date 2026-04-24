// crewday — SSE-related shared types.
//
// The canonical SSE event envelope + dispatcher lives at
// `@/lib/sse` (see `SseEvent`, `EventKind`, and `INVALIDATIONS`). That
// module owns the wire shape, parsing, and invalidation fan-out; this
// file is kept solely for the cross-cutting `AgentTurnScope` literal
// that both the SSE handlers and the product-side chat components
// (`components/AgentSidebar.tsx`, `lib/agentTyping.ts`) reuse.
//
// A discriminated-union `SseEvent` type previously lived here; it has
// been retired in favour of the lib-level `SseEvent` interface so the
// dispatcher, tests, and per-kind narrow casts all share a single
// source of truth.

export type AgentTurnScope = "employee" | "manager" | "admin" | "task";
