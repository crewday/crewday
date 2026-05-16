import { useEffect, useMemo, useState } from "react";
import type { DragEvent, FormEvent, KeyboardEvent, ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { GripVertical, Plus, Trash2 } from "lucide-react";
import ConfirmationModal from "@/components/ConfirmationModal";
import FormModal from "@/components/FormModal";
import SearchableSelect, { type SearchableSelectOption } from "@/components/SearchableSelect";
import { useReorderableList } from "@/components/useReorderableList";
import { Chip } from "@/components/common";
import { ApiError, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type {
  LlmAssignment,
  LlmCapabilityEntry,
  LlmGraphPayload,
  LlmProviderModel,
  LlmThinkingLevel,
} from "@/types";
import LlmUsageTotals from "./LlmUsageTotals";
import LlmPlayground from "./LlmPlayground";
import type { LlmIndexes } from "./lib/llmIndexes";

const DEFAULT_LLM_CAPABILITY = "default";
const INHERITANCE_FORM_ID = "llm-assignment-inheritance-form";

interface AssignmentModalProps {
  capabilityKey: string | null;
  graph: LlmGraphPayload;
  indexes: LlmIndexes;
  onClose: () => void;
}

interface AssignmentPayload {
  capability: string;
  provider_model_id: string;
  priority: number;
  max_tokens: number | null;
  temperature: number | null;
  thinking_level_override: LlmThinkingLevel | null;
  extra_api_params: Record<string, unknown>;
  required_capabilities: string[] | null;
  is_enabled: boolean;
}

type ThinkingOverrideValue = LlmThinkingLevel | "inherit";

const THINKING_LEVEL_OPTIONS = [
  "disabled",
  "low",
  "medium",
  "high",
] as const satisfies readonly LlmThinkingLevel[];

function apiErrorCopy(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const code =
      typeof error.problem?.error === "string" ? error.problem.error : undefined;
    if (code === "assignment_missing_capability") {
      return "That provider-model does not satisfy this capability. Choose a compatible model.";
    }
    if (code === "capability_inheritance_cycle") {
      return "That inheritance change would create a cycle. Choose a different parent.";
    }
    if (code === "capability_inheritance_self_loop") {
      return "A capability cannot inherit from itself.";
    }
    if (code === "default_capability_inheritance_forbidden") {
      return "The default capability must own the deployment fallback chain.";
    }
    if (code === "capability_inheritance_exists") {
      return "This capability already has an explicit parent. Change the existing parent instead.";
    }
    if (code === "capability_direct_assignments_exist") {
      return "This capability still has directly assigned models. Confirm replacement before creating inheritance.";
    }
    return error.detail ?? error.title ?? error.message ?? fallback;
  }
  return error instanceof Error ? error.message : fallback;
}

function compatibleMissing(
  providerModel: LlmProviderModel | undefined,
  required: string[],
  indexes: LlmIndexes,
): string[] {
  const model = providerModel ? indexes.modelsById.get(providerModel.model_id) : null;
  if (!model) return required;
  const tags = new Set(model.capabilities);
  return required.filter((tag) => !tags.has(tag));
}

function providerModelLabel(pm: LlmProviderModel, indexes: LlmIndexes): string {
  const model = indexes.modelsById.get(pm.model_id);
  const provider = indexes.providersById.get(pm.provider_id);
  return `${model?.display_name ?? pm.api_model_id} via ${provider?.name ?? "provider"}`;
}

function capabilityOption(capability: LlmCapabilityEntry): SearchableSelectOption {
  return {
    value: capability.key,
    label: capability.key,
    secondaryText: capability.description,
    searchText: [
      capability.key,
      capability.description,
      capability.required_capabilities.join(" "),
    ].join(" "),
  };
}

function sortedParentOptions(
  graph: LlmGraphPayload,
  capabilityKey: string | null,
  indexes: LlmIndexes,
): LlmCapabilityEntry[] {
  const childCounts = new Map<string, number>();
  for (const edge of graph.inheritance) {
    childCounts.set(
      edge.inherits_from,
      (childCounts.get(edge.inherits_from) ?? 0) + 1,
    );
  }
  return [
    ...graph.capabilities.filter(
      (cap) =>
        cap.key !== capabilityKey && !indexes.inheritanceByChild.has(cap.key),
    ),
  ].sort((left, right) => {
    const countDiff =
      (childCounts.get(right.key) ?? 0) - (childCounts.get(left.key) ?? 0);
    return countDiff || left.key.localeCompare(right.key);
  });
}

function thinkingLabel(level: LlmThinkingLevel): string {
  if (level === "disabled") return "Thinking off";
  return `Thinking ${level}`;
}

function isThinkingOverride(value: string): value is ThinkingOverrideValue {
  return value === "inherit" || (THINKING_LEVEL_OPTIONS as readonly string[]).includes(value);
}

function providerModelIsAvailable(pm: LlmProviderModel, indexes: LlmIndexes): boolean {
  const provider = indexes.providersById.get(pm.provider_id);
  const model = indexes.modelsById.get(pm.model_id);
  return pm.is_enabled && provider?.is_enabled === true && model?.is_active === true;
}

function assignmentCreatePayload(
  capability: LlmCapabilityEntry,
  providerModelId: string,
  priority: number,
  indexes: LlmIndexes,
): AssignmentPayload {
  const providerModel = indexes.pmById.get(providerModelId);
  const missing = compatibleMissing(
    providerModel,
    capability.required_capabilities,
    indexes,
  );
  if (missing.length) {
    throw new Error(
      `Selected provider-model is missing ${missing.join(", ")} for ${capability.key}.`,
    );
  }
  return {
    capability: capability.key,
    provider_model_id: providerModelId,
    priority,
    max_tokens: null,
    temperature: null,
    thinking_level_override: null,
    extra_api_params: {},
    required_capabilities: capability.required_capabilities,
    is_enabled: true,
  };
}

function directAssignments(
  capabilityKey: string | null,
  indexes: LlmIndexes,
): LlmAssignment[] {
  return capabilityKey ? (indexes.assignmentsByCapability.get(capabilityKey) ?? []) : [];
}

export default function LlmAssignmentModal({
  capabilityKey,
  graph,
  indexes,
  onClose,
}: AssignmentModalProps) {
  const qc = useQueryClient();
  const capability = capabilityKey
    ? indexes.capabilitiesByKey.get(capabilityKey)
    : undefined;
  const chain = useMemo(
    () => directAssignments(capabilityKey, indexes),
    [capabilityKey, indexes],
  );
  const explicitParent = capabilityKey
    ? indexes.explicitInheritanceByChild.get(capabilityKey)
    : undefined;
  const inheritedChildren = capabilityKey
    ? (indexes.childrenByParent.get(capabilityKey) ?? [])
    : [];
  const [inheritParent, setInheritParent] = useState("");
  const [draggedProviderModelId, setDraggedProviderModelId] = useState<string | null>(
    null,
  );
  const [replacementParent, setReplacementParent] = useState<string | null>(null);
  const [clientErr, setClientErr] = useState<string | null>(null);
  const [serverErr, setServerErr] = useState<string | null>(null);

  useEffect(() => {
    setInheritParent(explicitParent ?? "");
    setDraggedProviderModelId(null);
    setReplacementParent(null);
    setClientErr(null);
    setServerErr(null);
  }, [capabilityKey, explicitParent]);

  const invalidate = async () => {
    await qc.invalidateQueries({ queryKey: qk.adminLlmGraph() });
    await qc.invalidateQueries({ queryKey: qk.adminLlmCalls() });
  };

  const createAssignment = useMutation({
    mutationFn: (providerModelId: string) => {
      if (!capability) throw new Error("Capability is missing.");
      return fetchJson<LlmAssignment>("/admin/api/v1/llm/assignments", {
        method: "POST",
        body: assignmentCreatePayload(
          capability,
          providerModelId,
          chain.length,
          indexes,
        ),
      });
    },
    onSuccess: invalidate,
    onError: (error: Error) =>
      setServerErr(apiErrorCopy(error, "Assignment create failed.")),
  });

  const deleteAssignment = useMutation({
    mutationFn: (assignmentId: string) =>
      fetchJson(`/admin/api/v1/llm/assignments/${assignmentId}`, {
        method: "DELETE",
      }),
    onSuccess: invalidate,
    onError: (error: Error) =>
      setServerErr(apiErrorCopy(error, "Assignment delete failed.")),
  });

  const reorderAssignments = useMutation({
    mutationFn: (ids: string[]) =>
      fetchJson<LlmAssignment[]>("/admin/api/v1/llm/assignments/reorder", {
        method: "PATCH",
        body: [{ capability: capabilityKey, ids_in_priority_order: ids }],
      }),
    onSuccess: invalidate,
    onError: (error: Error) =>
      setServerErr(apiErrorCopy(error, "Assignment reorder failed.")),
  });

  const updateAssignmentThinking = useMutation({
    mutationFn: ({
      assignmentId,
      thinkingOverride,
    }: {
      assignmentId: string;
      thinkingOverride: ThinkingOverrideValue;
    }) =>
      fetchJson<LlmAssignment>(`/admin/api/v1/llm/assignments/${assignmentId}`, {
        method: "PUT",
        body: {
          thinking_level_override:
            thinkingOverride === "inherit" ? null : thinkingOverride,
        },
      }),
    onSuccess: invalidate,
    onError: (error: Error) =>
      setServerErr(apiErrorCopy(error, "Assignment thinking save failed.")),
  });

  const saveInheritance = useMutation({
    mutationFn: ({
      inheritsFrom,
      clearDirectAssignments,
    }: {
      inheritsFrom: string;
      clearDirectAssignments?: boolean;
    }) => {
      if (!capabilityKey) throw new Error("Capability is missing.");
      if (explicitParent) {
        return fetchJson(
          `/admin/api/v1/llm/inheritance/${encodeURIComponent(capabilityKey)}`,
          { method: "PUT", body: { inherits_from: inheritsFrom } },
        );
      }
      return fetchJson("/admin/api/v1/llm/inheritance", {
        method: "POST",
        body: {
          capability: capabilityKey,
          inherits_from: inheritsFrom,
          ...(clearDirectAssignments ? { clear_direct_assignments: true } : {}),
        },
      });
    },
    onSuccess: async () => {
      setReplacementParent(null);
      await invalidate();
    },
    onError: (error: Error) =>
      setServerErr(apiErrorCopy(error, "Inheritance save failed.")),
  });

  const deleteInheritance = useMutation({
    mutationFn: () => {
      if (!capabilityKey) throw new Error("Capability is missing.");
      return fetchJson(
        `/admin/api/v1/llm/inheritance/${encodeURIComponent(capabilityKey)}`,
        { method: "DELETE" },
      );
    },
    onSuccess: invalidate,
    onError: (error: Error) =>
      setServerErr(apiErrorCopy(error, "Inheritance delete failed.")),
  });

  const selectedProviderModelIds = new Set(
    chain.map((assignment) => assignment.provider_model_id),
  );
  const availableProviderModels = graph.provider_models.filter(
    (pm) => providerModelIsAvailable(pm, indexes) && !selectedProviderModelIds.has(pm.id),
  );
  const eligibleParentOptions = useMemo(
    () => sortedParentOptions(graph, capabilityKey, indexes),
    [capabilityKey, graph, indexes],
  );
  const directAssignmentNames = chain.map((assignment) => {
    const pm = indexes.pmById.get(assignment.provider_model_id);
    return pm ? providerModelLabel(pm, indexes) : assignment.provider_model_id;
  });
  const err = clientErr ?? serverErr;
  const titleId = capabilityKey ? "llm-assignment-modal-title" : undefined;
  const pending =
    createAssignment.isPending ||
    deleteAssignment.isPending ||
    reorderAssignments.isPending ||
    updateAssignmentThinking.isPending ||
    saveInheritance.isPending ||
    deleteInheritance.isPending;
  const playgroundAssignment = chain[0];
  const playgroundProviderModel = playgroundAssignment
    ? indexes.pmById.get(playgroundAssignment.provider_model_id)
    : undefined;
  const playgroundModel = playgroundProviderModel
    ? indexes.modelsById.get(playgroundProviderModel.model_id)
    : undefined;

  function addProviderModel(providerModelId: string): void {
    if (!capability) return;
    setClientErr(null);
    setServerErr(null);
    if (selectedProviderModelIds.has(providerModelId)) return;
    try {
      assignmentCreatePayload(capability, providerModelId, chain.length, indexes);
    } catch (error) {
      setClientErr(error instanceof Error ? error.message : "Assignment is invalid.");
      return;
    }
    createAssignment.mutate(providerModelId);
  }

  function moveAssignmentTo(assignmentId: string, toIndex: number): void {
    const fromIndex = chain.findIndex((assignment) => assignment.id === assignmentId);
    if (fromIndex < 0 || fromIndex === toIndex) return;
    const next = [...chain];
    const [moved] = next.splice(fromIndex, 1);
    next.splice(toIndex, 0, moved!);
    reorderAssignments.mutate(next.map((assignment) => assignment.id));
  }

  function submitInheritance(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setClientErr(null);
    setServerErr(null);
    if (!capabilityKey) return;
    if (!inheritParent) {
      setClientErr("Choose a parent capability before saving inheritance.");
      return;
    }
    if (inheritParent === capabilityKey) {
      setClientErr("A capability cannot inherit from itself.");
      return;
    }
    if (!explicitParent && chain.length > 0) {
      setReplacementParent(inheritParent);
      return;
    }
    saveInheritance.mutate({ inheritsFrom: inheritParent });
  }

  return (
    <>
      <FormModal
        open={capabilityKey !== null}
        title={capabilityKey ?? "Capability"}
        titleId={titleId}
        eyebrow={explicitParent ? "Inherited assignment chain" : "Direct assignment chain"}
        width="wide"
        bodyClassName="llm-assignment-dialog__body"
        footerClassName={
          capability && explicitParent
            ? "llm-assignment-dialog__inheritance-footer"
            : undefined
        }
        contentElement="section"
        onClose={onClose}
        actions={
          capability && explicitParent ? (
            <>
              <button
                type="button"
                className="btn btn--ghost"
                disabled={pending}
                onClick={() => {
                  setClientErr(null);
                  setServerErr(null);
                  deleteInheritance.mutate();
                }}
              >
                Remove inheritance
              </button>
              <button
                type="submit"
                form={INHERITANCE_FORM_ID}
                className="btn btn--moss"
                disabled={pending || !inheritParent}
              >
                Change inheritance
              </button>
            </>
          ) : null
        }
      >
        <section
          className="llm-assignment-dialog__content"
          aria-busy={pending}
        >
          {capability ? <CapabilityHeader capability={capability} /> : null}
          {err ? (
            <p className="form-error" role="alert">
              {err}
            </p>
          ) : null}
          {capability && explicitParent ? (
            <InheritanceOnlyPanel
              capability={capability}
              explicitParent={explicitParent}
              chain={indexes.assignmentsByCapability.get(explicitParent) ?? []}
              indexes={indexes}
              inheritedChildren={inheritedChildren}
              inheritParent={inheritParent}
              eligibleParentOptions={eligibleParentOptions}
              pending={pending}
              onParentChange={setInheritParent}
              onSubmit={submitInheritance}
            />
          ) : capability ? (
            <>
              <DirectChainPicker
                capability={capability}
                chain={chain}
                availableProviderModels={availableProviderModels}
                indexes={indexes}
                pending={pending}
                draggedProviderModelId={draggedProviderModelId}
                onAdd={addProviderModel}
                onDelete={(assignmentId) => {
                  setClientErr(null);
                  setServerErr(null);
                  deleteAssignment.mutate(assignmentId);
                }}
                onMoveTo={moveAssignmentTo}
                onThinkingChange={(assignmentId, thinkingOverride) => {
                  setClientErr(null);
                  setServerErr(null);
                  updateAssignmentThinking.mutate({ assignmentId, thinkingOverride });
                }}
                onProviderModelDrag={setDraggedProviderModelId}
              />
              <CreateInheritancePanel
                capability={capability}
                inheritParent={inheritParent}
                eligibleParentOptions={eligibleParentOptions}
                pending={pending}
                onParentChange={setInheritParent}
                onSubmit={submitInheritance}
              />
              {playgroundAssignment && playgroundProviderModel ? (
                <LlmPlayground
                  providerModel={playgroundProviderModel}
                  model={playgroundModel}
                  mode="assignment"
                  assignment={playgroundAssignment}
                  titleId="llm-assignment-playground-title"
                  description="Run a stateless smoke test through this assignment. Assignment tuning applies automatically."
                />
              ) : null}
            </>
          ) : null}
        </section>
      </FormModal>
      <ConfirmationModal
        open={replacementParent !== null}
        title="Replace direct assignment chain?"
        confirmLabel="Create inheritance"
        pending={saveInheritance.isPending}
        onCancel={() => {
          if (!saveInheritance.isPending) setReplacementParent(null);
        }}
        onConfirm={() => {
          if (!replacementParent) return;
          setClientErr(null);
          setServerErr(null);
          saveInheritance.mutate({
            inheritsFrom: replacementParent,
            clearDirectAssignments: true,
          });
        }}
      >
        <p>
          Creating inheritance for <code>{capabilityKey}</code> will remove{" "}
          {chain.length} directly assigned {chain.length === 1 ? "model" : "models"}.
        </p>
        <ul>
          {directAssignmentNames.map((name) => (
            <li key={name}>{name}</li>
          ))}
        </ul>
      </ConfirmationModal>
    </>
  );
}

function CapabilityHeader({ capability }: { capability: LlmCapabilityEntry }) {
  return (
    <div className="llm-assignment-dialog__intro">
      <div className="llm-assignment-dialog__intro-main">
        <p>{capability.description}</p>
        <div className="llm-assignment-dialog__chips" aria-label="Required capabilities">
          {capability.required_capabilities.map((tag) => (
            <Chip key={tag} tone="sky" size="sm">
              {tag}
            </Chip>
          ))}
        </div>
      </div>
      <LlmUsageTotals
        spendUsd={capability.spend_usd_30d}
        calls={capability.calls_30d}
      />
    </div>
  );
}

function InheritanceOnlyPanel({
  capability,
  explicitParent,
  chain,
  indexes,
  inheritedChildren,
  inheritParent,
  eligibleParentOptions,
  pending,
  onParentChange,
  onSubmit,
}: {
  capability: LlmCapabilityEntry;
  explicitParent: string;
  chain: LlmAssignment[];
  indexes: LlmIndexes;
  inheritedChildren: string[];
  inheritParent: string;
  eligibleParentOptions: LlmCapabilityEntry[];
  pending: boolean;
  onParentChange: (parent: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  const parentOptions = useMemo(
    () => eligibleParentOptions.map(capabilityOption),
    [eligibleParentOptions],
  );

  return (
    <form
      id={INHERITANCE_FORM_ID}
      className="llm-assignment-dialog__inheritance"
      onSubmit={onSubmit}
    >
      <div className="llm-assignment-dialog__section-head">
        <div>
          <h4>Inheritance</h4>
          <p>
            <code className="inline-code">{capability.key}</code> inherits the chain
            owned by <code className="inline-code">{explicitParent}</code>.
          </p>
        </div>
      </div>
      <InheritedChainSummary
        chain={chain}
        childCapability={capability}
        indexes={indexes}
      />
      {inheritedChildren.length ? (
        <p>Children: {inheritedChildren.join(", ")}</p>
      ) : null}
      <SearchableSelect
        label="Change inheritance"
        requirement="required"
        className="form-modal__field"
        value={inheritParent}
        options={parentOptions}
        onChange={onParentChange}
        disabled={pending}
        required
        blankOption={{ label: "Choose a parent capability" }}
        placeholder="Search parent capabilities..."
      />
    </form>
  );
}

function InheritedChainSummary({
  chain,
  childCapability,
  indexes,
}: {
  chain: LlmAssignment[];
  childCapability: LlmCapabilityEntry;
  indexes: LlmIndexes;
}) {
  if (!chain.length) {
    return (
      <p className="llm-assignment-dialog__empty">
        The parent has no direct assignment chain.
      </p>
    );
  }
  return (
    <ol className="llm-assignment-chain-summary">
      {chain.map((assignment) => {
        const pm = indexes.pmById.get(assignment.provider_model_id);
        const missing = compatibleMissing(
          pm,
          childCapability.required_capabilities,
          indexes,
        );
        return (
          <li key={assignment.id} className="llm-assignment-chain-summary__item">
            <ProviderModelSummary
              variant="available"
              providerModel={pm}
              indexes={indexes}
              missing={missing}
            />
          </li>
        );
      })}
    </ol>
  );
}

function CreateInheritancePanel({
  capability,
  inheritParent,
  eligibleParentOptions,
  pending,
  onParentChange,
  onSubmit,
}: {
  capability: LlmCapabilityEntry;
  inheritParent: string;
  eligibleParentOptions: LlmCapabilityEntry[];
  pending: boolean;
  onParentChange: (parent: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  if (capability.key === DEFAULT_LLM_CAPABILITY) {
    return null;
  }

  return (
    <form className="llm-assignment-dialog__inheritance" onSubmit={onSubmit}>
      <div>
        <h4>Inheritance</h4>
        <p>
          Create an explicit parent only for capabilities that should use another
          direct chain instead of owning one here.
        </p>
      </div>
      <SearchableSelect
        label="Parent capability"
        requirement="required"
        className="form-modal__field"
        value={inheritParent}
        options={eligibleParentOptions.map(capabilityOption)}
        onChange={onParentChange}
        disabled={pending}
        required
        blankOption={{ label: "No explicit parent" }}
        placeholder="Search parent capabilities..."
      />
      <div className="llm-assignment-dialog__inherit-actions">
        <button
          type="submit"
          className="btn btn--ghost"
          disabled={pending || !inheritParent}
        >
          Create inheritance
        </button>
      </div>
    </form>
  );
}

function DirectChainPicker({
  capability,
  chain,
  availableProviderModels,
  indexes,
  pending,
  draggedProviderModelId,
  onAdd,
  onDelete,
  onMoveTo,
  onThinkingChange,
  onProviderModelDrag,
}: {
  capability: LlmCapabilityEntry;
  chain: LlmAssignment[];
  availableProviderModels: LlmProviderModel[];
  indexes: LlmIndexes;
  pending: boolean;
  draggedProviderModelId: string | null;
  onAdd: (providerModelId: string) => void;
  onDelete: (assignmentId: string) => void;
  onMoveTo: (assignmentId: string, toIndex: number) => void;
  onThinkingChange: (
    assignmentId: string,
    thinkingOverride: ThinkingOverrideValue,
  ) => void;
  onProviderModelDrag: (providerModelId: string | null) => void;
}) {
  const reorder = useReorderableList({
    items: chain,
    getId: (assignment) => assignment.id,
    onMove: onMoveTo,
    disabled: pending,
    defaultDropPosition: "before",
  });
  const selectedListProps = reorder.getListProps();

  function handleSelectedDrop(event: DragEvent<HTMLOListElement>): void {
    if (draggedProviderModelId) {
      event.preventDefault();
      onAdd(draggedProviderModelId);
      onProviderModelDrag(null);
      return;
    }
    selectedListProps.onDrop(event);
  }

  function handleSelectedDragOver(event: DragEvent<HTMLOListElement>): void {
    if (draggedProviderModelId) event.preventDefault();
  }

  function handleAvailableDrop(event: DragEvent<HTMLDivElement>): void {
    event.preventDefault();
    if (reorder.draggedId) onDelete(reorder.draggedId);
    reorder.clearDragState();
    onProviderModelDrag(null);
  }

  function handleAvailableDragOver(event: DragEvent<HTMLElement>): void {
    if (reorder.draggedId) event.preventDefault();
  }

  return (
    <div className="llm-assignment-picker" aria-label="Direct provider-model chain">
      <section
        className="llm-assignment-picker__column"
        onDragOver={handleAvailableDragOver}
        onDrop={handleAvailableDrop}
      >
        <div className="llm-assignment-dialog__section-head">
          <div>
            <h4>Available provider-models</h4>
            <p>Enabled provider-models not already in this chain.</p>
          </div>
        </div>
        <ul className="llm-assignment-picker__list">
          {availableProviderModels.map((pm) => {
            const missing = compatibleMissing(
              pm,
              capability.required_capabilities,
              indexes,
            );
            const incompatible = missing.length > 0;
            return (
              <li key={pm.id}>
                <ProviderModelRow
                  providerModel={pm}
                  indexes={indexes}
                  missing={missing}
                  variant="available"
                  draggable={!incompatible && !pending}
                  disabled={incompatible || pending}
                  actionLabel="Add"
                  onDragStart={(event) => {
                    onProviderModelDrag(pm.id);
                    event.dataTransfer.effectAllowed = "copy";
                    event.dataTransfer.setData("text/plain", pm.id);
                  }}
                  onDragEnd={() => onProviderModelDrag(null)}
                  onAction={() => onAdd(pm.id)}
                />
              </li>
            );
          })}
        </ul>
      </section>
      <section className="llm-assignment-picker__column">
        <div className="llm-assignment-dialog__section-head">
          <div>
            <h4>Selected chain</h4>
            <p>Top to bottom is the runtime fallback order.</p>
          </div>
        </div>
        {chain.length ? (
          <ol
            className="llm-assignment-picker__list"
            onDragOver={handleSelectedDragOver}
            onDragLeave={selectedListProps.onDragLeave}
            onDrop={handleSelectedDrop}
          >
            {chain.map((assignment, index) => {
              const pm = indexes.pmById.get(assignment.provider_model_id);
              const missing = compatibleMissing(
                pm,
                capability.required_capabilities,
                indexes,
              );
              const label = providerModelLabelOrUnknown(pm, indexes);
              const itemProps = reorder.getItemProps(index);
              const isDragging = reorder.draggedId === assignment.id;
              const dropPosition =
                reorder.dropTarget?.id === assignment.id
                  ? reorder.dropTarget.position
                  : null;
              return (
                <li
                  key={assignment.id}
                  className={[
                    "llm-assignment-picker__selected",
                    isDragging ? "llm-assignment-picker__selected--dragging" : "",
                    dropPosition
                      ? `llm-assignment-picker__selected--drop-${dropPosition}`
                      : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  onDragOver={itemProps.onDragOver}
                  onDragLeave={itemProps.onDragLeave}
                  onDrop={itemProps.onDrop}
                >
                  <ProviderModelRow
                    providerModel={pm}
                    indexes={indexes}
                    missing={missing}
                    variant="selected"
                    draggable={itemProps.draggable}
                    disabled={pending}
                    actionLabel="Remove"
                    ariaLabel={`${label}, selected chain position ${index + 1}`}
                    onDragStart={itemProps.onDragStart}
                    onDragEnd={itemProps.onDragEnd}
                    onKeyDown={(event) => {
                      if (!event.altKey) return;
                      if (event.key === "ArrowUp" && index > 0) {
                        event.preventDefault();
                        onMoveTo(assignment.id, index - 1);
                      }
                      if (event.key === "ArrowDown" && index < chain.length - 1) {
                        event.preventDefault();
                        onMoveTo(assignment.id, index + 1);
                      }
                    }}
                    onAction={() => onDelete(assignment.id)}
                  >
                    <AssignmentThinkingSelect
                      assignment={assignment}
                      label={label}
                      disabled={pending}
                      onChange={(thinkingOverride) =>
                        onThinkingChange(assignment.id, thinkingOverride)
                      }
                    />
                  </ProviderModelRow>
                </li>
              );
            })}
          </ol>
        ) : (
          <div
            className="llm-assignment-dialog__empty"
            onDragOver={(event) => {
              if (draggedProviderModelId) event.preventDefault();
            }}
            onDrop={(event) => {
              event.preventDefault();
              if (draggedProviderModelId) onAdd(draggedProviderModelId);
              onProviderModelDrag(null);
            }}
          >
            No direct provider-models selected.
          </div>
        )}
      </section>
    </div>
  );
}

function ProviderModelRow({
  providerModel,
  indexes,
  missing,
  variant,
  draggable,
  disabled,
  actionLabel,
  ariaLabel,
  onDragStart,
  onDragEnd,
  onKeyDown,
  onAction,
  children,
}: {
  providerModel: LlmProviderModel | undefined;
  indexes: LlmIndexes;
  missing: string[];
  variant: "available" | "selected";
  draggable: boolean;
  disabled: boolean;
  actionLabel: "Add" | "Remove";
  ariaLabel?: string;
  onDragStart: (event: DragEvent<HTMLElement>) => void;
  onDragEnd: (event: DragEvent<HTMLElement>) => void;
  onKeyDown?: (event: KeyboardEvent<HTMLElement>) => void;
  onAction: () => void;
  children?: ReactNode;
}) {
  const label = providerModelLabelOrUnknown(providerModel, indexes);
  return (
    <article
      className={[
        "llm-assignment-provider-model",
        `llm-assignment-provider-model--${variant}`,
        missing.length ? "is-incompatible" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      tabIndex={variant === "selected" && draggable ? 0 : undefined}
      aria-label={ariaLabel}
      aria-keyshortcuts={variant === "selected" ? "Alt+ArrowUp Alt+ArrowDown" : undefined}
      draggable={draggable}
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onKeyDown={onKeyDown}
    >
      <span className="llm-assignment-provider-model__handle" aria-hidden="true">
        <GripVertical size={15} />
      </span>
      <span className="llm-assignment-provider-model__content">
        <ProviderModelSummary
          variant={variant}
          providerModel={providerModel}
          indexes={indexes}
          missing={missing}
        />
        {children}
      </span>
      <button
        type="button"
        className="btn btn--ghost btn--sm"
        disabled={disabled}
        onClick={onAction}
        aria-label={`${actionLabel} ${label}`}
        title={`${actionLabel} ${label}`}
      >
        {actionLabel === "Add" ? (
          <Plus size={16} aria-hidden="true" />
        ) : (
          <Trash2 size={16} aria-hidden="true" />
        )}
      </button>
    </article>
  );
}

function ProviderModelSummary({
  variant,
  providerModel,
  indexes,
  missing,
}: {
  variant: "available" | "selected";
  providerModel: LlmProviderModel | undefined;
  indexes: LlmIndexes;
  missing: string[];
}) {
  if (!providerModel) {
    return <span className="llm-assignment-provider-model__main">Unknown provider-model</span>;
  }
  const model = indexes.modelsById.get(providerModel.model_id);
  const provider = indexes.providersById.get(providerModel.provider_id);
  return (
    <span className="llm-assignment-provider-model__main">
      <strong>{model?.display_name ?? "Unknown model"}</strong>
      {variant === "available" ? (
        <span className="llm-assignment-provider-model__meta">
          {provider?.name ?? "Provider"} ·{" "}
          {thinkingLabel(model?.thinking_level ?? "disabled")}
        </span>
      ) : null}
      {missing.length ? (
        <span className="llm-assignment-provider-model__chips">
          <Chip tone="rust" size="sm">
            missing {missing.join(", ")}
          </Chip>
        </span>
      ) : null}
    </span>
  );
}

function AssignmentThinkingSelect({
  assignment,
  label,
  disabled,
  onChange,
}: {
  assignment: LlmAssignment;
  label: string;
  disabled: boolean;
  onChange: (thinkingOverride: ThinkingOverrideValue) => void;
}) {
  const value: ThinkingOverrideValue = assignment.thinking_level_override ?? "inherit";
  return (
    <label className="field form-field form-field--optional llm-assignment-provider-model__thinking">
      <select
        aria-label={`Thinking override for ${label}`}
        value={value}
        disabled={disabled}
        onChange={(event) => {
          const next = event.target.value;
          if (isThinkingOverride(next)) onChange(next);
        }}
      >
        <option value="inherit">
          Inherit ({thinkingLabel(assignment.effective_thinking_level)})
        </option>
        {THINKING_LEVEL_OPTIONS.map((level) => (
          <option key={level} value={level}>
            {thinkingLabel(level)}
          </option>
        ))}
      </select>
    </label>
  );
}

function providerModelLabelOrUnknown(
  providerModel: LlmProviderModel | undefined,
  indexes: LlmIndexes,
): string {
  return providerModel ? providerModelLabel(providerModel, indexes) : "provider-model";
}
