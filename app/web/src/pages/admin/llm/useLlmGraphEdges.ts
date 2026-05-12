import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
} from "react";
import type { LlmGraphPayload } from "@/types";
import type { LlmIndexes } from "./lib/llmIndexes";
import type { EdgeLayout, Selection } from "./types";

export function useLlmGraphEdges(
  graph: LlmGraphPayload | undefined,
  indexes: LlmIndexes | null,
  active: Selection | null,
) {
  const graphRef = useRef<HTMLDivElement | null>(null);
  const providerRefs = useRef<Map<string, HTMLElement>>(new Map());
  const modelRefs = useRef<Map<string, HTMLElement>>(new Map());
  const providerModelRefs = useRef<Map<string, HTMLElement>>(new Map());
  const rungRefs = useRef<Map<string, HTMLElement>>(new Map());
  const frameRef = useRef<number | null>(null);
  const [edges, setEdges] = useState<EdgeLayout[]>([]);
  const [canvas, setCanvas] = useState<{ w: number; h: number }>({ w: 0, h: 0 });

  const recomputeEdges = useCallback(() => {
    const host = graphRef.current;
    if (!host || !graph || !indexes) return;
    const hostBox = host.getBoundingClientRect();
    setCanvas({ w: hostBox.width, h: hostBox.height });
    const next: EdgeLayout[] = [];
    const issues = new Set(graph.assignment_issues.map((i) => i.assignment_id));
    for (const pm of graph.provider_models) {
      const provider = providerRefs.current.get(pm.provider_id);
      const providerModel = providerModelRefs.current.get(pm.id);
      if (!provider || !providerModel) continue;
      const pBox = provider.getBoundingClientRect();
      const pmBox = providerModel.getBoundingClientRect();
      const x1 = pBox.right - hostBox.left;
      const y1 = pBox.top + pBox.height / 2 - hostBox.top;
      const x2 = pmBox.left - hostBox.left;
      const y2 = pmBox.top + pmBox.height / 2 - hostBox.top;
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
      const providerModel = providerModelRefs.current.get(a.provider_model_id);
      const rung = rungRefs.current.get(a.id);
      if (!providerModel || !rung) continue;
      const pmBox = providerModel.getBoundingClientRect();
      const rBox = rung.getBoundingClientRect();
      const x1 = pmBox.right - hostBox.left;
      const y1 = pmBox.top + pmBox.height / 2 - hostBox.top;
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
  }, [graph, indexes]);

  const requestRecompute = useCallback(() => {
    if (frameRef.current !== null) window.cancelAnimationFrame(frameRef.current);
    frameRef.current = window.requestAnimationFrame(() => {
      frameRef.current = null;
      recomputeEdges();
    });
  }, [recomputeEdges]);

  const setRef = useCallback(
    (map: RefObject<Map<string, HTMLElement>>) =>
      (id: string) =>
      (el: HTMLElement | null) => {
        if (el) map.current.set(id, el);
        else map.current.delete(id);
        requestRecompute();
      },
    [requestRecompute],
  );

  useLayoutEffect(() => {
    requestRecompute();
  }, [active, graph, requestRecompute]);

  useEffect(() => {
    if (!graphRef.current) return;
    const ro = new ResizeObserver(() => requestRecompute());
    const observeAll = () => {
      const host = graphRef.current;
      if (host) ro.observe(host);
      for (const node of providerRefs.current.values()) ro.observe(node);
      for (const node of modelRefs.current.values()) ro.observe(node);
      for (const node of providerModelRefs.current.values()) ro.observe(node);
      for (const node of rungRefs.current.values()) ro.observe(node);
    };
    observeAll();
    const onWinResize = () => requestRecompute();
    const onScroll = () => requestRecompute();
    window.addEventListener("resize", onWinResize);
    window.addEventListener("scroll", onScroll, true);
    requestRecompute();
    return () => {
      if (frameRef.current !== null) window.cancelAnimationFrame(frameRef.current);
      ro.disconnect();
      window.removeEventListener("resize", onWinResize);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [graph, indexes, requestRecompute]);

  return {
    graphRef,
    providerRefs,
    modelRefs,
    providerModelRefs,
    rungRefs,
    edges,
    canvas,
    setRef,
  };
}
