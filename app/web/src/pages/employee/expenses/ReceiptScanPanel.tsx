import { useCallback } from "react";
import { Search } from "lucide-react";
import { fetchJson } from "@/lib/api";
import type { ExpenseScanResult } from "@/types/api";

// Phase machine matches the mock verbatim: `upload` shows the receipt
// picker, `processing` swaps to a spinner with a deliberate 1.5 s
// minimum so the OCR feels considered (not a flash of "did anything
// happen?"), then the parent transitions to `review`.
//
// The panel only owns the file-picker DOM and the request lifecycle —
// the parent handles `phase` so the upload pane and the review form
// can share a single state machine without prop-drilling.

interface Props {
  /**
   * Drives which slot renders. The panel itself is a no-op for
   * `review` and `submitted` (the parent's other panels take over),
   * but we keep the prop intentionally narrow so the parent can pass
   * the full union without massaging it.
   */
  phase: "upload" | "processing" | "review" | "submitted";
  /**
   * Called once the OCR call resolves with a parsed result. The parent
   * folds the result into the review form's initial state and flips to
   * the `review` phase.
   */
  onScanResult: (result: ExpenseScanResult) => void;
  /** Flips to `processing` the instant the user picks a file. */
  onScanStarted: () => void;
}

export default function ReceiptScanPanel({
  phase,
  onScanResult,
  onScanStarted,
}: Props) {
  const handleFileSelect = useCallback(async () => {
    onScanStarted();
    // The minimum-wait promise keeps the spinner visible long enough
    // to register as "we read your receipt", even when the OCR call
    // returns in <100 ms (e.g. when the LLM cache hits). Mock parity.
    const minWait = new Promise<void>((r) => setTimeout(r, 1500));
    const scan = fetchJson<ExpenseScanResult>("/api/v1/expenses/scan", {
      method: "POST",
    });
    const [result] = await Promise.all([scan, minWait]);
    onScanResult(result);
  }, [onScanResult, onScanStarted]);

  if (phase === "upload") {
    return (
      <>
        <h2 className="section-title">Submit an expense</h2>
        <label className="evidence__picker" tabIndex={0}>
          <input
            type="file"
            accept="image/*"
            capture="environment"
            onChange={handleFileSelect}
          />
          <span className="evidence__picker-cta">
            Scan a receipt or screenshot
          </span>
          <span className="evidence__picker-sub">
            Photo, payment confirmation, or bank transfer
          </span>
        </label>
      </>
    );
  }

  if (phase === "processing") {
    return (
      <div className="empty-state">
        <span className="empty-state__glyph" aria-hidden="true">
          <Search size={28} strokeWidth={1.8} />
        </span>
        Reading your receipt...
      </div>
    );
  }

  return null;
}
