import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { LlmGraphPayload } from "@/types";
import type { LlmIndexes } from "./lib/llmIndexes";
import type { EdgeLayout } from "./types";

export function useLlmGraphEdges(
  graph: LlmGraphPayload | undefined,
  indexes: LlmIndexes | null,
) {
  const graphRef = useRef<HTMLDivElement | null>(null);
  const providerRefs = useRef<Map<string, HTMLElement>>(new Map());
  const modelRefs = useRef<Map<string, HTMLElement>>(new Map());
  const rungRefs = useRef<Map<string, HTMLElement>>(new Map());
  const [edges, setEdges] = useState<EdgeLayout[]>([]);
  const [canvas, setCanvas] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
  const setRef = (map: typeof providerRefs) => (id: string) => (el: HTMLElement | null) => {
    if (el) map.current.set(id, el);
    else map.current.delete(id);
  };

  const recomputeEdges = () => {
    const host = graphRef.current;
    if (!host || !graph || !indexes) return;
    const hostBox = host.getBoundingClientRect();
    setCanvas({ w: hostBox.width, h: hostBox.height });
    const next: EdgeLayout[] = [];
    const issues = new Set(graph.assignment_issues.map((i) => i.assignment_id));
    for (const pm of graph.provider_models) {
      const provider = providerRefs.current.get(pm.provider_id);
      const model = modelRefs.current.get(pm.model_id);
      if (!provider || !model) continue;
      const pBox = provider.getBoundingClientRect();
      const mBox = model.getBoundingClientRect();
      const x1 = pBox.right - hostBox.left;
      const y1 = pBox.top + pBox.height / 2 - hostBox.top;
      const x2 = mBox.left - hostBox.left;
      const y2 = mBox.top + mBox.height / 2 - hostBox.top;
      const dx = Math.max(40, (x2 - x1) * 0.55);
      next.push({
        id: "pm-" + pm.id,
        kind: "pm",
        providerId: pm.provider_id,
        modelId: pm.model_id,
        providerModelId: pm.id,
        d: `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`,
        invalid: false,
      });
    }
    for (const a of graph.assignments) {
      const pm = indexes.pmById.get(a.provider_model_id);
      if (!pm) continue;
      const model = modelRefs.current.get(pm.model_id);
      const rung = rungRefs.current.get(a.id);
      if (!model || !rung) continue;
      const mBox = model.getBoundingClientRect();
      const rBox = rung.getBoundingClientRect();
      const x1 = mBox.right - hostBox.left;
      const y1 = mBox.top + mBox.height / 2 - hostBox.top;
      const x2 = rBox.left - hostBox.left;
      const y2 = rBox.top + rBox.height / 2 - hostBox.top;
      const dx = Math.max(40, (x2 - x1) * 0.55);
      next.push({
        id: "a-" + a.id,
        kind: "assign",
        providerId: pm.provider_id,
        modelId: pm.model_id,
        providerModelId: pm.id,
        assignmentId: a.id,
        capability: a.capability,
        d: `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`,
        invalid: issues.has(a.id),
      });
    }
    setEdges(next);
  };

  useLayoutEffect(() => {
    recomputeEdges();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph]);

  useEffect(() => {
    if (!graphRef.current) return;
    const ro = new ResizeObserver(() => recomputeEdges());
    ro.observe(graphRef.current);
    const onWinResize = () => recomputeEdges();
    const onScroll = () => recomputeEdges();
    window.addEventListener("resize", onWinResize);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", onWinResize);
      window.removeEventListener("scroll", onScroll, true);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph]);

  return {
    graphRef,
    providerRefs,
    modelRefs,
    rungRefs,
    edges,
    canvas,
    setRef,
  };
}
