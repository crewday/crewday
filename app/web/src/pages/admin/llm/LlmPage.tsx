import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import DeskPage from "@/components/DeskPage";
import { Loading } from "@/components/common";
import SearchField from "@/components/SearchField";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { LlmGraphPayload } from "@/types";
import type { MouseEvent } from "react";
import AssignmentColumn from "./AssignmentColumn";
import LlmAssignmentModal from "./LlmAssignmentModal";
import LlmAlerts from "./LlmAlerts";
import LlmRegistryModals from "./LlmRegistryModals";
import ModelColumn from "./ModelColumn";
import ProviderColumn from "./ProviderColumn";
import { buildHighlighted, emptyHighlighted } from "./lib/highlight";
import { buildLlmGraphLayout } from "./lib/graphLayout";
import { filterLlmGraphBySearch, selectionIsVisible } from "./lib/graphSearch";
import { buildLlmIndexes } from "./lib/llmIndexes";
import { useLlmGraphEdges } from "./useLlmGraphEdges";
import type { Column, EdgeLayout, Selection } from "./types";
import type { RegistryDialogState } from "./LlmRegistryModals";

const sub =
  "Deployment-wide LLM graph/config: providers, models, and capability assignment chains. Shared by every workspace.";
const title = "LLM graph";

export default function AdminLlmPage() {
  // code-health: ignore[nloc] LLM graph route already delegates columns, alerts, registry modals, assignments, and drawers.
  const graphQ = useQuery({
    queryKey: qk.adminLlmGraph(),
    queryFn: () => fetchJson<LlmGraphPayload>("/admin/api/v1/llm/graph"),
  });

  const [selection, setSelection] = useState<Selection | null>(null);
  const [hover, setHover] = useState<Selection | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const searchRef = useRef<HTMLInputElement | null>(null);
  const [registryDialog, setRegistryDialog] = useState<RegistryDialogState | null>(
    null,
  );
  const [assignmentDialogCapability, setAssignmentDialogCapability] = useState<
    string | null
  >(null);

  const sourceGraph = graphQ.data;
  const sourceIndexes = useMemo(
    () => (sourceGraph ? buildLlmIndexes(sourceGraph) : null),
    [sourceGraph],
  );
  const searchResult = useMemo(
    () =>
      sourceGraph && sourceIndexes
        ? filterLlmGraphBySearch(sourceGraph, sourceIndexes, searchQuery)
        : null,
    [sourceGraph, sourceIndexes, searchQuery],
  );
  const graph = searchResult?.graph ?? sourceGraph;
  const indexes = useMemo(() => (graph ? buildLlmIndexes(graph) : null), [graph]);
  const layout = useMemo(
    () => (graph && indexes ? buildLlmGraphLayout(graph, indexes) : null),
    [graph, indexes],
  );
  const visibleSet = useMemo(() => {
    if (!graph) return emptyHighlighted();
    return {
      providers: new Set(graph.providers.map((item) => item.id)),
      models: new Set(graph.models.map((item) => item.id)),
      providerModels: new Set(graph.provider_models.map((item) => item.id)),
      assignments: new Set(graph.assignments.map((item) => item.id)),
      capabilities: new Set(graph.capabilities.map((item) => item.key)),
    };
  }, [graph]);
  const visibleHover = selectionIsVisible(hover, visibleSet) ? hover : null;
  const visibleSelection = selectionIsVisible(selection, visibleSet) ? selection : null;
  const active = visibleHover ?? visibleSelection;

  const highlighted = useMemo(() => {
    if (!graph || !indexes || !active) return emptyHighlighted();
    return buildHighlighted(graph, indexes, active);
  }, [graph, indexes, active]);
  const mutedPath = useMemo(() => {
    if (!graph || !indexes || !selection) return emptyHighlighted();
    return buildHighlighted(graph, indexes, selection);
  }, [graph, indexes, selection]);

  const hasSelection = visibleSelection !== null;
  const {
    graphRef,
    providerRefs,
    modelRefs,
    providerModelRefs,
    rungRefs,
    edges,
    canvas,
    setRef,
  } = useLlmGraphEdges(graph, indexes, active);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.defaultPrevented) return;
      const target = event.target;
      if (
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target instanceof HTMLSelectElement ||
        (target instanceof HTMLElement && target.isContentEditable)
      ) {
        return;
      }
      event.preventDefault();
      searchRef.current?.focus();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const edgeIsOnPath = (e: EdgeLayout, source: Selection | null): boolean => {
    if (!source) return false;
    if (source.column === "provider") return e.providerId === source.id;
    if (source.column === "model") return e.modelId === source.id;
    if (source.column === "providerModel") return e.providerModelId === source.id;
    if (source.column === "assignment") {
      if (e.kind === "assign") return e.assignmentId === source.id;
      return (
        e.providerModelId ===
          graph?.assignments.find((x) => x.id === source.id)?.provider_model_id
      );
    }
    if (source.column === "capability") {
      if (e.kind === "assign") return e.capability === source.id;
      const chain = indexes?.assignmentsByCapability.get(source.id) ?? [];
      return chain.some((a) => a.provider_model_id === e.providerModelId);
    }
    return false;
  };

  const nodeClass = (col: Column, id: string) => {
    const set = {
      provider: highlighted.providers,
      model: highlighted.models,
      providerModel: highlighted.providerModels,
      assignment: highlighted.assignments,
      capability: highlighted.capabilities,
    }[col];
    const mutedSet = {
      provider: mutedPath.providers,
      model: mutedPath.models,
      providerModel: mutedPath.providerModels,
      assignment: mutedPath.assignments,
      capability: mutedPath.capabilities,
    }[col];
    const isOn = set.has(id);
    const isActive = active?.column === col && active.id === id;
    const isSearchMatch =
      searchResult?.hasQuery &&
      {
        provider: searchResult.directMatches.providers,
        model: searchResult.directMatches.models,
        providerModel: searchResult.directMatches.providerModels,
        assignment: searchResult.directMatches.assignments,
        capability: searchResult.directMatches.capabilities,
      }[col].has(id);
    const dim = hasSelection && !mutedSet.has(id);
    return [
      "llm-graph-node",
      `llm-graph-node--${col}`,
      isActive ? "is-active" : "",
      isOn && !isActive ? "is-linked" : "",
      isSearchMatch ? "is-search-match" : "",
      dim ? "is-dim" : "",
    ]
      .filter(Boolean)
      .join(" ");
  };

  const openCapabilityDialog = (capability: string) => {
    setSelection({ column: "capability", id: capability });
    setAssignmentDialogCapability(capability);
  };

  const openAssignmentDialog = (assignmentId: string) => {
    const assignment = sourceGraph?.assignments.find((item) => item.id === assignmentId);
    if (!assignment) return;
    setSelection({ column: "assignment", id: assignmentId });
    setAssignmentDialogCapability(assignment.capability);
  };

  const clearSelectionFromGraphBackground = (event: MouseEvent<HTMLDivElement>) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (
      target.closest(
        [
          "a",
          "button",
          "input",
          "select",
          "textarea",
          "[role='button']",
          "[role='menuitem']",
          ".llm-graph-node",
          ".llm-graph-node__child",
          ".llm-graph-chain__rung",
          ".llm-graph__col-header",
        ].join(","),
      )
    ) {
      return;
    }
    setHover(null);
    setSelection(null);
  };

  if (graphQ.isPending) {
    return (
      <DeskPage title={title} sub={sub}>
        <Loading />
      </DeskPage>
    );
  }
  if (!sourceGraph || !sourceIndexes || !graph || !indexes || !layout) {
    return (
      <DeskPage title={title} sub={sub}>
        Failed to load.
      </DeskPage>
    );
  }

  return (
    <DeskPage title={title} sub={sub}>
      <div className="llm-graph-page">
        <LlmAlerts graph={graph} syncResult={undefined} />

        <div className="llm-graph-toolbar">
          <SearchField
            ref={searchRef}
            label="Search LLM graph"
            placeholder="Providers, models, assignments, capabilities"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.currentTarget.value)}
          />
        </div>

        <div
          className="llm-graph"
          ref={graphRef}
          onClick={clearSelectionFromGraphBackground}
        >
          <svg
            className="llm-graph__edges"
            width={canvas.w}
            height={canvas.h}
            aria-hidden="true"
          >
            {edges.map((e) => {
              const highlighted = edgeIsOnPath(e, active);
              const dim = hasSelection && !edgeIsOnPath(e, visibleSelection);
              const cls = [
                "llm-graph__edge",
                `llm-graph__edge--${e.kind}`,
                highlighted ? "is-linked" : "",
                dim ? "is-dim" : "",
                e.invalid ? "is-error" : "",
              ]
                .filter(Boolean)
                .join(" ");
              return <path key={e.id} className={cls} d={e.d} />;
            })}
          </svg>

          <div className="llm-graph__col-header llm-graph__col-header--providers">
            <div className="llm-graph__col-heading">
              <span className="llm-graph__col-title">Providers</span>
              <span className="llm-graph__col-count">{graph.providers.length}</span>
            </div>
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() => setRegistryDialog({ kind: "provider", mode: "create" })}
            >
              + New provider
            </button>
          </div>
          <div className="llm-graph__col-header llm-graph__col-header--models">
            <div className="llm-graph__col-heading">
              <span className="llm-graph__col-title">Models</span>
              <span className="llm-graph__col-count">{graph.models.length}</span>
            </div>
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() => setRegistryDialog({ kind: "model", mode: "create" })}
            >
              + New model
            </button>
          </div>
          <div className="llm-graph__col-header llm-graph__col-header--assignments">
            <div className="llm-graph__col-heading">
              <span className="llm-graph__col-title">Assignments</span>
              <span className="llm-graph__col-count">
                {graph.capabilities.length}
              </span>
            </div>
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() =>
                setRegistryDialog({ kind: "providerModel", mode: "create" })
              }
            >
              + New provider-model
            </button>
          </div>

          <ProviderColumn
            providers={layout.providers}
            setHover={setHover}
            setSelection={setSelection}
            onEditProvider={(id) =>
              setRegistryDialog({ kind: "provider", mode: "edit", id })
            }
            nodeClass={nodeClass}
            setProviderRef={setRef(providerRefs)}
          />
          <ModelColumn
            models={layout.models}
            setHover={setHover}
            setSelection={setSelection}
            nodeClass={nodeClass}
            setModelRef={setRef(modelRefs)}
            setProviderModelRef={setRef(providerModelRefs)}
            onEditModel={(id) => setRegistryDialog({ kind: "model", mode: "edit", id })}
            onEditProviderModel={(id) =>
              setRegistryDialog({ kind: "providerModel", mode: "edit", id })
            }
            indexes={indexes}
            providerModelsByModelId={layout.providerModelsByModelId}
          />
          <AssignmentColumn
            assignmentGroups={layout.assignmentGroups}
            indexes={indexes}
            active={active}
            setHover={setHover}
            setSelection={setSelection}
            nodeClass={nodeClass}
            hasSelection={hasSelection}
            highlighted={highlighted}
            mutedPath={mutedPath}
            searchMatches={searchResult?.hasQuery ? searchResult.directMatches : null}
            setRungRef={setRef(rungRefs)}
            onOpenCapability={openCapabilityDialog}
            onOpenAssignment={openAssignmentDialog}
            onOpenProviderModel={(id) =>
              setRegistryDialog({ kind: "providerModel", mode: "edit", id })
            }
          />
          {searchResult?.hasQuery && !searchResult.hasMatches ? (
            <div className="llm-graph__empty" role="status">
              No providers, models, assignments, or capabilities match "{searchQuery}".
            </div>
          ) : null}
        </div>

        <LlmRegistryModals
          dialog={registryDialog}
          providers={sourceGraph.providers}
          models={sourceGraph.models}
          providerModels={sourceGraph.provider_models}
          indexes={sourceIndexes}
          onOpenProviderModel={(providerModel) =>
            setRegistryDialog({
              kind: "providerModel",
              mode: "edit",
              id: providerModel.id,
              providerModel,
            })
          }
          onClose={() => {
            setHover(null);
            setSelection(null);
            setRegistryDialog(null);
          }}
        />
        <LlmAssignmentModal
          capabilityKey={assignmentDialogCapability}
          graph={sourceGraph}
          indexes={sourceIndexes}
          onClose={() => {
            setHover(null);
            setSelection(null);
            setAssignmentDialogCapability(null);
          }}
        />
      </div>
    </DeskPage>
  );
}
