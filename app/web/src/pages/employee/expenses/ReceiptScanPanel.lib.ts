import { ApiError } from "@/lib/api";

/**
 * Map a thrown error from `fetchJson` to a plain-English line for
 * the worker. The server's RFC 7807 `type` is the discriminator; we
 * fall back to the response detail / generic message so a brand-new
 * code still surfaces something readable rather than "undefined".
 */
export function messageForScanError(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.type) {
      case "scan_not_configured":
        return "Receipt scanning isn't enabled here yet, add the expense by hand for now.";
      case "blob_mime_not_allowed":
        return "We can't read that format yet, try a JPEG, PNG, WebP, HEIC, or PDF.";
      case "blob_too_large":
        return "That file is too large, keep it under 10 MB.";
      case "blob_empty":
        return "That file looked empty. Try the picker again.";
      case "extraction_timeout":
      case "extraction_rate_limited":
      case "extraction_provider_error":
      case "extraction_parse_error":
      case "extraction_invariant":
        return "Our reader is having a moment, try again in a few seconds, or add it by hand.";
      default:
        return (
          err.detail ??
          err.title ??
          "We couldn't read that receipt. Try again, or add it by hand."
        );
    }
  }
  return "We couldn't read that receipt. Try again, or add it by hand.";
}
