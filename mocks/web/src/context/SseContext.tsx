import { useEffect, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { startEventStream } from "@/lib/sse";

export function SseProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  useEffect(() => startEventStream(qc), [qc]);
  return <>{children}</>;
}
