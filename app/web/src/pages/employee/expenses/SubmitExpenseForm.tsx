import { useCallback, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import {
  buildExpenseClaimCreatePayload,
  fetchActiveEngagementId,
  type ExpenseClaimCreatePayload,
} from "@/lib/expenses";
import type {
  Expense,
  ExpenseScanResult,
  Me,
  Property,
} from "@/types/api";
import AgentQuestionPrompt from "./AgentQuestionPrompt";
import {
  deriveConfidences,
  deriveValues,
  type FieldConfidences,
  type FieldValues,
} from "./lib/scanDerivation";
import { SubmitExpenseFields } from "./SubmitExpenseFields";

// Worker's review form. Two entry paths:
// - **Scan** — `initialScan` carries an `ExpenseScanResult`; high-
//   confidence fields seed the form, low-confidence fields stay blank,
//   and a soft amber ring highlights the uncertain band ([0.6, 0.9)).
// - **Manual** — `initialScan` is null; every field starts blank and
//   nothing is decorated.
//
// Form state lives entirely inside the component because the parent
// only cares about `phase` transitions; the field values never leak
// upward. On a successful create the form fires `onSubmitted()` so the
// parent flips the phase, then resets its own internal state for the
// next open.
//
// Lifecycle: the parent unmounts this component on every transition out
// of the `review` phase (Back, Submitted, manual entry → review with a
// new scan). That guarantees a fresh `useState` initialiser on every
// re-entry, so we deliberately do NOT mirror `initialScan` into local
// state via `useEffect` — that would clobber a partially-typed value if
// a future parent ever swapped the scan reference mid-review (and would
// also fire a redundant extra render on every mount).
//
// **Wire contract.** The submit POST matches `ExpenseClaimCreate`
// (`app/domain/expenses/claims.py:417`) — `vendor`, `purchased_at`,
// `total_amount_cents`, `currency`, `category`, optional `property_id`,
// optional `note_md`, plus a `work_engagement_id` resolved from the
// caller's active engagement via `/api/v1/work_engagements`. The
// projection from form state to wire shape lives in
// `buildExpenseClaimCreatePayload` so the test suite can pin the
// boundary without spinning up a React tree.

interface Props {
  initialScan: ExpenseScanResult | null;
  onSubmitted: () => void;
  onBack: () => void;
}

/**
 * Append a non-empty agent-question reply to the note body. Returns the
 * note unchanged when the reply is blank, so the helper is safe to call
 * unconditionally on submit and on dismiss.
 */
function foldReply(note: string, reply: string): string {
  const trimmed = reply.trim();
  if (!trimmed) return note;
  return note ? `${note}\n\nReply: ${trimmed}` : trimmed;
}

export default function SubmitExpenseForm({
  initialScan,
  onSubmitted,
  onBack,
}: Props) {
  // code-health: ignore[ccn nloc] Query/mutation orchestration remains here after field rendering was extracted.
  const qc = useQueryClient();
  const isScanned = initialScan !== null;

  const [values, setValues] = useState<FieldValues>(() => deriveValues(initialScan));
  const [conf] = useState<FieldConfidences>(() => deriveConfidences(initialScan));
  const [agentReply, setAgentReply] = useState("");
  const [questionDismissed, setQuestionDismissed] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // The wire contract requires `work_engagement_id`. The `/me`
  // payload deliberately omits it (a user with multiple workspaces
  // shouldn't pay the join cost on every page load), so we fetch the
  // caller's active engagement on mount. The form is unsubmittable
  // until the lookup resolves; `null` (no active engagement) flips
  // the form into a read-only "no engagement" state rather than
  // surfacing an opaque 422 from the server.
  const meQ = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });
  const myUserId = meQ.data?.user_id ?? null;
  const engagementQ = useQuery({
    enabled: myUserId !== null,
    queryKey: qk.workEngagementActive(myUserId ?? ""),
    queryFn: () =>
      fetchActiveEngagementId(
        // Narrowed by `enabled` above; the cast keeps strict-mode happy
        // without a non-null assertion mid-call.
        myUserId as string,
      ),
  });
  const workEngagementId = engagementQ.data ?? null;

  // Properties drive the optional `property_id` dropdown. Worker pages
  // already use `qk.properties()` (HistoryPage, mocks parity) so the
  // cache hit is shared.
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  const create = useMutation({
    mutationFn: (payload: ExpenseClaimCreatePayload) =>
      fetchJson<Expense>("/api/v1/expenses", { method: "POST", body: payload }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.expenses("mine") });
      onSubmitted();
    },
    onError: (err: Error) => {
      setSubmitError(err.message);
    },
  });

  const setField = useCallback(<K extends keyof FieldValues>(
    key: K,
    value: FieldValues[K],
  ) => {
    setValues((prev) => ({ ...prev, [key]: value }));
  }, []);

  const dismissQuestion = useCallback(() => {
    setValues((prev) => ({ ...prev, note_md: foldReply(prev.note_md, agentReply) }));
    setQuestionDismissed(true);
  }, [agentReply]);

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      setSubmitError(null);
      if (!workEngagementId) {
        setSubmitError(
          "No active work engagement — ask a manager to seed one before submitting expenses.",
        );
        return;
      }
      // Fold an outstanding agent-question reply into the note before
      // the network call so the server stores the worker's full
      // context. Mirrors the mock.
      const finalNote = questionDismissed
        ? values.note_md
        : foldReply(values.note_md, agentReply);
      let payload: ExpenseClaimCreatePayload;
      try {
        payload = buildExpenseClaimCreatePayload({
          work_engagement_id: workEngagementId,
          vendor: values.vendor,
          purchased_on: values.purchased_on,
          amount: values.amount,
          currency: values.currency,
          category: values.category,
          property_id: values.property_id,
          note_md: finalNote,
        });
      } catch (err) {
        setSubmitError(err instanceof Error ? err.message : String(err));
        return;
      }
      create.mutate(payload);
    },
    [values, agentReply, questionDismissed, create, workEngagementId],
  );

  const agentQuestion = initialScan?.agent_question ?? null;
  const showQuestion = agentQuestion && !questionDismissed;
  const engagementResolving = meQ.isPending || engagementQ.isPending;
  const engagementLookupFailed = meQ.isError || engagementQ.isError;
  const noActiveEngagement =
    !engagementResolving && !engagementLookupFailed && workEngagementId === null;

  return (
    <>
      <h2 className="section-title">
        {isScanned ? "Review scanned expense" : "New expense"}
      </h2>

      {showQuestion && (
        <AgentQuestionPrompt
          question={agentQuestion}
          reply={agentReply}
          onReplyChange={setAgentReply}
          onDismiss={dismissQuestion}
        />
      )}

      <form className="form" onSubmit={handleSubmit}>
        <SubmitExpenseFields
          values={values}
          confidences={conf}
          isScanned={isScanned}
          properties={propsQ.data ?? []}
          onFieldChange={setField}
        />

        {noActiveEngagement && (
          <p className="form__error" role="alert">
            No active work engagement found in this workspace. Ask a manager to
            set one up before submitting expenses.
          </p>
        )}
        {engagementLookupFailed && (
          <p className="form__error" role="alert">
            Couldn't look up your active work engagement. Refresh to try again.
          </p>
        )}
        {submitError && (
          <p className="form__error" role="alert">
            {submitError}
          </p>
        )}

        <div className="form__row">
          <button type="button" className="btn btn--ghost" onClick={onBack}>
            Back
          </button>
          <button
            type="submit"
            className="btn btn--moss"
            disabled={
              create.isPending
              || engagementResolving
              || engagementLookupFailed
              || noActiveEngagement
            }
          >
            Submit expense
          </button>
        </div>
      </form>
    </>
  );
}
