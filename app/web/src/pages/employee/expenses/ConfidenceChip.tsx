import { Chip } from "@/components/common";

// Soft-amber percentage chip shown next to a field label when OCR
// confidence sits in the "review me" band ([0.6, 0.9)). Outside the
// band the chip is omitted (the value is either trusted or left blank,
// so the chip would just be visual noise).
export default function ConfidenceChip({
  isScanned,
  confidence,
}: {
  isScanned: boolean;
  confidence: number | null;
}) {
  if (!isScanned || confidence == null) return null;
  if (confidence < 0.6 || confidence >= 0.9) return null;
  return <Chip tone="sand" size="sm">{Math.round(confidence * 100)}%</Chip>;
}
