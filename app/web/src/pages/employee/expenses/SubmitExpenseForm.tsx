import { useCallback, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import type { Expense, ExpenseScanResult } from "@/types/api";
import AgentQuestionPrompt from "./AgentQuestionPrompt";
import ConfidenceChip from "./ConfidenceChip";
import { CATEGORIES, confidenceClass } from "./lib/expenseHelpers";
import {
  deriveConfidences,
  deriveValues,
  minConfidence,
  type FieldConfidences,
  type FieldValues,
} from "./lib/scanDerivation";

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
  const qc = useQueryClient();
  const isScanned = initialScan !== null;

  const [values, setValues] = useState<FieldValues>(() => deriveValues(initialScan));
  const [conf] = useState<FieldConfidences>(() => deriveConfidences(initialScan));
  const [agentReply, setAgentReply] = useState("");
  const [questionDismissed, setQuestionDismissed] = useState(false);

  const create = useMutation({
    mutationFn: (payload: {
      merchant: string;
      amount: string;
      currency: string;
      category: string;
      note: string;
      ocr_confidence: number | null;
    }) =>
      fetchJson<Expense>("/api/v1/expenses", { method: "POST", body: payload }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.expenses("mine") });
      onSubmitted();
    },
  });

  const setField = useCallback(<K extends keyof FieldValues>(
    key: K,
    value: FieldValues[K],
  ) => {
    setValues((prev) => ({ ...prev, [key]: value }));
  }, []);

  const dismissQuestion = useCallback(() => {
    setValues((prev) => ({ ...prev, note: foldReply(prev.note, agentReply) }));
    setQuestionDismissed(true);
  }, [agentReply]);

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      // Fold an outstanding agent-question reply into the note before
      // the network call so the server stores the worker's full
      // context. Mirrors the mock.
      const finalNote =
        questionDismissed ? values.note : foldReply(values.note, agentReply);
      create.mutate({
        merchant: values.merchant,
        amount: values.amount,
        currency: values.currency,
        category: values.category,
        note: finalNote,
        ocr_confidence: minConfidence(conf),
      });
    },
    [values, conf, agentReply, questionDismissed, create],
  );

  const agentQuestion = initialScan?.agent_question ?? null;
  const showQuestion = agentQuestion && !questionDismissed;

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
        <label className={`field ${confidenceClass(conf.merchant)}`}>
          <span>
            Merchant
            <ConfidenceChip isScanned={isScanned} confidence={conf.merchant} />
          </span>
          <input
            name="merchant"
            placeholder="e.g. Carrefour"
            required
            value={values.merchant}
            onChange={(e) => setField("merchant", e.target.value)}
          />
        </label>

        <div className="form__row">
          <label className={`field field--grow ${confidenceClass(conf.amount)}`}>
            <span>
              Amount
              <ConfidenceChip isScanned={isScanned} confidence={conf.amount} />
            </span>
            <input
              name="amount"
              type="number"
              step="0.01"
              placeholder="0.00"
              required
              value={values.amount}
              onChange={(e) => setField("amount", e.target.value)}
            />
          </label>
          <label className="field field--currency">
            <span>Currency</span>
            <input
              name="currency"
              value={values.currency}
              onChange={(e) => setField("currency", e.target.value)}
            />
          </label>
        </div>

        <div className={`field ${confidenceClass(conf.category)}`}>
          <span>
            Category
            <ConfidenceChip isScanned={isScanned} confidence={conf.category} />
          </span>
          <div className="chip-group">
            {CATEGORIES.map((c) => (
              <label key={c.value} className="chip-radio">
                <input
                  type="radio"
                  name="category"
                  value={c.value}
                  checked={values.category === c.value}
                  onChange={() => setField("category", c.value)}
                />
                <span>{c.label}</span>
              </label>
            ))}
          </div>
        </div>

        <label className={`field ${confidenceClass(conf.note)}`}>
          <span>
            Note
            <ConfidenceChip isScanned={isScanned} confidence={conf.note} />
          </span>
          <AutoGrowTextarea
            name="note"
            placeholder="What it was for"
            value={values.note}
            onChange={(e) => setField("note", e.target.value)}
          />
        </label>

        <div className="form__row">
          <button type="button" className="btn btn--ghost" onClick={onBack}>
            Back
          </button>
          <button
            type="submit"
            className="btn btn--moss"
            disabled={create.isPending}
          >
            Submit expense
          </button>
        </div>
      </form>
    </>
  );
}
