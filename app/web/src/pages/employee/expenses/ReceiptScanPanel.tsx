import { useCallback, useLayoutEffect, useState } from "react";
import { Search } from "lucide-react";
import FileDropZone from "@/components/FileDropZone";
import { EmptyState } from "@/components/common";
import { fetchJson } from "@/lib/api";
import type { ExpenseScanResult } from "@/types/api";
import { messageForScanError } from "./ReceiptScanPanel.lib";

// Phase machine matches the mock verbatim: `upload` shows the receipt
// picker, `processing` swaps to a spinner with a deliberate 1.5 s
// minimum so the OCR feels considered (not a flash of "did anything
// happen?"), then the parent transitions to `review`.
//
// The panel only owns the file-picker DOM and the request lifecycle,
// the parent handles `phase` so the upload pane and the review form
// can share a single state machine without prop-drilling.
//
// Wire contract (spec §12 §expenses):
//   POST /api/v1/expenses/scan, multipart/form-data, field `image`.
//   Server allow-list: image/jpeg, image/png, image/webp, image/heic,
//   application/pdf (≤ 10 MB). Errors arrive as RFC 7807 with the
//   short `type` keys mapped below, we surface a plain-English line
//   per code rather than dumping the raw `detail`, since the worker
//   shouldn't have to read "blob_mime_not_allowed" to understand
//   "we can't read this format yet". `extraction_*` codes from the
//   LLM side (timeout, rate-limited, provider error, parse error,
//   invariant) are folded into a single retry message, the worker's
//   action ("try again in a moment, or add it by hand") is identical
//   regardless of which provider hiccup landed.

// Mirrors the server's `_SCAN_ALLOWED_MIME` allow-list verbatim
// (`app/api/v1/expenses.py`). Kept as a module-level constant so a
// drift on the server triggers a corresponding edit here, not a
// silent client/server mismatch where the picker accepts a file the
// server then rejects with 422.
const ACCEPTED_MIMES: ReadonlySet<string> = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/heic",
  "application/pdf",
]);

// String list for the <input accept="…"> attribute. Native pickers
// honour this as a hint (they may still surface "all files" on some
// platforms); we re-validate after selection regardless.
const ACCEPT_ATTR = [...ACCEPTED_MIMES].join(",");

// Spinner-floor so the OCR feels considered even on cache hits. Runs
// on the failure path too so a fast 422 doesn't flash the picker
// state in and out.
const MIN_SPINNER_MS = 1500;

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
  /**
   * Called when the scan fails. The parent reverts to `upload` so the
   * panel can re-render its picker with an inline error notice.
   * Optional so older call sites still compile during the staged
   * rollout, but every production caller wires this, without it the
   * spinner would stay visible after a 422.
   */
  onScanFailed?: () => void;
}

export default function ReceiptScanPanel({
  phase,
  onScanResult,
  onScanStarted,
  onScanFailed,
}: Props) {
  const [error, setError] = useState<string | null>(null);

  // Drop the inline error the moment the worker leaves the picker,
  // otherwise a failed scan, then "+ New expense" → "Back", would
  // show a stale error against an unrelated fresh picker. We only
  // clear on the transition *out* of `upload`; the in-flight failure
  // path itself keeps the error visible because the parent flips
  // from `processing` straight back to `upload` (where the notice
  // belongs).
  useLayoutEffect(() => {
    if (phase !== "upload" && phase !== "processing") {
      setError(null);
    }
  }, [phase]);

  const handleFileSelect = useCallback(
    async (files: File[]) => {
      const file = files[0] ?? null;
      if (!file) return;

      if (!ACCEPTED_MIMES.has(file.type)) {
        // Some platforms hand us a blank `type` for HEIC; rather than
        // guess from the extension we surface the same notice and
        // let the worker re-pick, the server's allow-list is the
        // source of truth.
        setError(
          "We can't read that format yet, try a JPEG, PNG, WebP, HEIC, or PDF.",
        );
        return;
      }

      setError(null);
      onScanStarted();

      const form = new FormData();
      form.append("image", file);

      // The minimum-wait promise keeps the spinner visible long
      // enough to register as "we read your receipt", even when the
      // OCR call returns in <100 ms (e.g. when the LLM cache hits).
      // We wait alongside both success and failure so a fast 422
      // doesn't pop the picker in/out.
      const minWait = new Promise<void>((r) => setTimeout(r, MIN_SPINNER_MS));
      try {
        const scan = fetchJson<ExpenseScanResult>("/api/v1/expenses/scan", {
          method: "POST",
          body: form,
        });
        const [result] = await Promise.all([scan, minWait]);
        onScanResult(result);
      } catch (err) {
        await minWait;
        setError(messageForScanError(err));
        onScanFailed?.();
      }
    },
    [onScanResult, onScanStarted, onScanFailed],
  );

  if (phase === "upload") {
    return (
      <>
        <h2 className="section-title">Submit an expense</h2>
        {error && (
          <p className="evidence__error" role="alert">
            {error}
          </p>
        )}
        <FileDropZone
          className="evidence__picker"
          title="Scan a receipt or screenshot"
          description="Photo, payment confirmation, or bank transfer"
          accept={ACCEPT_ATTR}
          capture="environment"
          onFiles={handleFileSelect}
        />
      </>
    );
  }

  if (phase === "processing") {
    return (
      <EmptyState
        icon={Search}
        title="Reading your receipt"
        copy="The scan result will appear here for review."
      />
    );
  }

  return null;
}
