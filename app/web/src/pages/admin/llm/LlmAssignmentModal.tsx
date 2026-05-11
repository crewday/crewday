import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import FormModal, {
  FormModalField,
  FormModalGrid,
} from "@/components/FormModal";
import { Chip } from "@/components/common";
import { ApiError, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type {
  LlmAssignment,
  LlmCapabilityEntry,
  LlmGraphPayload,
  LlmProviderModel,
} from "@/types";
import LlmUsageTotals from "./LlmUsageTotals";
import type { LlmIndexes } from "./lib/llmIndexes";

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
  extra_api_params: Record<string, unknown>;
  required_capabilities: string[] | null;
  is_enabled: boolean;
}

interface AssignmentUpdatePayload {
  provider_model_id: string;
  max_tokens: number | null;
  temperature: number | null;
  extra_api_params: Record<string, unknown>;
  required_capabilities: string[] | null;
  is_enabled: boolean;
}

interface DraftRung {
  key: string;
  id: string | null;
  providerModelId: string;
  maxTokens: string;
  temperature: string;
  extraApiParams: string;
  requiredCapabilities: string;
  enabled: boolean;
  readOnly: boolean;
}

type SaveAssignmentInput =
  | { kind: "create"; draft: DraftRung; priority: number }
  | { kind: "update"; draft: DraftRung };

interface AssignmentPayloadInput {
  draft: DraftRung;
  capability: LlmCapabilityEntry;
  priority: number;
  indexes: LlmIndexes;
}

function formatJson(value: Record<string, unknown>): string {
  // code-health: ignore[ccn nloc] Lizard misattributes later modal branches to this tiny JSON display helper.
  return Object.keys(value).length ? JSON.stringify(value, null, 2) : "";
}

function apiErrorCopy(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const code =
      typeof error.problem?.error === "string" ? error.problem.error : undefined;
    if (code === "assignment_missing_capability") {
      return "That provider-model does not satisfy the capability requirements. Choose a compatible model before saving.";
    }
    if (code === "capability_inheritance_cycle") {
      return "That inheritance change would create a cycle. Choose a different parent.";
    }
    if (code === "capability_inheritance_self_loop") {
      return "A capability cannot inherit from itself.";
    }
    if (code === "capability_inheritance_exists") {
      return "This capability already has an explicit parent. Change the existing parent instead.";
    }
    return error.detail ?? error.title ?? error.message ?? fallback;
  }
  return error instanceof Error ? error.message : fallback;
}

function assignmentToDraft(assignment: LlmAssignment): DraftRung {
  return {
    key: assignment.id,
    id: assignment.id,
    providerModelId: assignment.provider_model_id,
    maxTokens: assignment.max_tokens === null ? "" : String(assignment.max_tokens),
    temperature:
      assignment.temperature === null ? "" : String(assignment.temperature),
    extraApiParams: formatJson(assignment.extra_api_params),
    requiredCapabilities: assignment.required_capabilities.join(", "),
    enabled: assignment.is_enabled,
    readOnly: assignment.is_deployment_default === true,
  };
}

function newDraft(
  capability: LlmCapabilityEntry,
  providerModelId: string,
): DraftRung {
  return {
    key: `new-${Date.now()}-${Math.random().toString(36).slice(2)}`,
    id: null,
    providerModelId,
    maxTokens: "",
    temperature: "",
    extraApiParams: "",
    requiredCapabilities: capability.required_capabilities.join(", "),
    enabled: true,
    readOnly: false,
  };
}

function optionalInt(value: string, label: string): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error(`${label} must be a whole number greater than zero.`);
  }
  return parsed;
}

function optionalTemperature(value: string): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 2) {
    throw new Error("Temperature must be between 0 and 2.");
  }
  return parsed;
}

function parseRequiredCapabilities(value: string): string[] | null {
  const tags = value
    .split(",")
    .map((tag) => tag.trim())
    .filter(Boolean);
  return tags.length ? [...new Set(tags)] : null;
}

function parseExtraApiParams(value: string): Record<string, unknown> {
  if (value.trim() === "") return {};
  const parsed = JSON.parse(value) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Extra API params must be a JSON object.");
  }
  return parsed as Record<string, unknown>;
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

function assignmentPayload(input: AssignmentPayloadInput): AssignmentPayload {
  const { draft, capability, priority, indexes } = input;
  const requiredCapabilities =
    parseRequiredCapabilities(draft.requiredCapabilities) ??
    capability.required_capabilities;
  const providerModel = indexes.pmById.get(draft.providerModelId);
  const missing = compatibleMissing(providerModel, requiredCapabilities, indexes);
  if (missing.length) {
    throw new Error(
      `Selected provider-model is missing ${missing.join(", ")} for ${capability.key}.`,
    );
  }
  return {
    capability: capability.key,
    provider_model_id: draft.providerModelId,
    priority,
    max_tokens: optionalInt(draft.maxTokens, "Max tokens"),
    temperature: optionalTemperature(draft.temperature),
    extra_api_params: parseExtraApiParams(draft.extraApiParams),
    required_capabilities: requiredCapabilities,
    is_enabled: draft.enabled,
  };
}

function providerModelLabel(pm: LlmProviderModel, indexes: LlmIndexes): string {
  const model = indexes.modelsById.get(pm.model_id);
  const provider = indexes.providersById.get(pm.provider_id);
  return `${model?.display_name ?? pm.api_model_id} via ${provider?.name ?? "provider"} (${pm.api_model_id})`;
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
    () =>
      capabilityKey ? (indexes.assignmentsByCapability.get(capabilityKey) ?? []) : [],
    [capabilityKey, indexes],
  );
  const explicitParent = capabilityKey
    ? indexes.explicitInheritanceByChild.get(capabilityKey)
    : undefined;
  const effectiveParent = capabilityKey
    ? indexes.inheritanceByChild.get(capabilityKey)
    : undefined;
  const inheritedChildren = capabilityKey
    ? (indexes.childrenByParent.get(capabilityKey) ?? [])
    : [];

  const [drafts, setDrafts] = useState<DraftRung[]>([]);
  const [inheritParent, setInheritParent] = useState("");
  const [clientErr, setClientErr] = useState<string | null>(null);
  const [serverErr, setServerErr] = useState<string | null>(null);

  useEffect(() => {
    setDrafts(chain.map(assignmentToDraft));
    setInheritParent(explicitParent ?? "");
    setClientErr(null);
    setServerErr(null);
  }, [capabilityKey, chain, explicitParent]);

  const invalidate = async () => {
    await qc.invalidateQueries({ queryKey: qk.adminLlmGraph() });
    await qc.invalidateQueries({ queryKey: qk.adminLlmCalls() });
  };

  const saveAssignment = useMutation({
    mutationFn: (input: SaveAssignmentInput) => {
      if (!capability) throw new Error("Capability is missing.");
      if (input.kind === "create") {
        const payload = assignmentPayload({
          draft: input.draft,
          capability,
          priority: input.priority,
          indexes,
        });
        return fetchJson<LlmAssignment>("/admin/api/v1/llm/assignments", {
          method: "POST",
          body: payload,
        });
      }
      if (!input.draft.id) throw new Error("Assignment id is missing.");
      const payload = assignmentPayload({
        draft: input.draft,
        capability,
        priority: 0,
        indexes,
      });
      const update: AssignmentUpdatePayload = {
        provider_model_id: payload.provider_model_id,
        max_tokens: payload.max_tokens,
        temperature: payload.temperature,
        extra_api_params: payload.extra_api_params,
        required_capabilities: payload.required_capabilities,
        is_enabled: payload.is_enabled,
      };
      return fetchJson<LlmAssignment>(
        `/admin/api/v1/llm/assignments/${input.draft.id}`,
        { method: "PUT", body: update },
      );
    },
    onSuccess: invalidate,
    onError: (error: Error) =>
      setServerErr(apiErrorCopy(error, "Assignment save failed.")),
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

  const saveInheritance = useMutation({
    mutationFn: (inheritsFrom: string) => {
      if (!capabilityKey) throw new Error("Capability is missing.");
      if (explicitParent) {
        return fetchJson(
          `/admin/api/v1/llm/inheritance/${encodeURIComponent(capabilityKey)}`,
          { method: "PUT", body: { inherits_from: inheritsFrom } },
        );
      }
      return fetchJson("/admin/api/v1/llm/inheritance", {
        method: "POST",
        body: { capability: capabilityKey, inherits_from: inheritsFrom },
      });
    },
    onSuccess: invalidate,
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

  const providerModelGroups = useMemo(() => {
    return graph.models.map((model) => ({
      model,
      providerModels: graph.provider_models.filter((pm) => pm.model_id === model.id),
    }));
  }, [graph.models, graph.provider_models]);

  const parentOptions = graph.capabilities.filter((cap) => cap.key !== capabilityKey);
  const err = clientErr ?? serverErr;
  const titleId = capabilityKey ? "llm-assignment-modal-title" : undefined;
  const hasReadOnlyDefault = drafts.some((draft) => draft.readOnly);

  function updateDraft(key: string, patch: Partial<DraftRung>): void {
    // code-health: ignore[ccn nloc] Lizard misattributes later assignment-editor JSX to this tiny draft updater.
    setDrafts((current) =>
      current.map((draft) => (draft.key === key ? { ...draft, ...patch } : draft)),
    );
  }

  function removeDraft(key: string): void {
    setClientErr(null);
    setServerErr(null);
    setDrafts((current) => current.filter((draft) => draft.key !== key));
  }

  function addRung(): void {
    if (!capability) return;
    const firstCompatible =
      graph.provider_models.find(
        (pm) =>
          compatibleMissing(pm, capability.required_capabilities, indexes).length === 0,
      ) ?? graph.provider_models[0];
    if (!firstCompatible) {
      setClientErr("Add a provider-model before creating assignments.");
      return;
    }
    setDrafts((current) => [...current, newDraft(capability, firstCompatible.id)]);
  }

  function submitRung(event: FormEvent<HTMLFormElement>, draft: DraftRung): void {
    event.preventDefault();
    if (draft.readOnly) return;
    if (!capability) return;
    setClientErr(null);
    setServerErr(null);
    try {
      assignmentPayload({
        draft,
        capability,
        priority: drafts.indexOf(draft),
        indexes,
      });
    } catch (error) {
      setClientErr(error instanceof Error ? error.message : "Assignment is invalid.");
      return;
    }
    saveAssignment.mutate(
      draft.id
        ? { kind: "update", draft }
        : { kind: "create", draft, priority: drafts.indexOf(draft) },
    );
  }

  function moveRung(index: number, direction: -1 | 1): void {
    const nextIndex = index + direction;
    const persisted = drafts.filter((draft) => draft.id && !draft.readOnly);
    if (
      nextIndex < 0 ||
      nextIndex >= drafts.length ||
      drafts.some((draft) => !draft.id || draft.readOnly) ||
      persisted.length !== drafts.length
    ) {
      setClientErr("Save new rungs before reordering, and deployment-default rows cannot be reordered.");
      return;
    }
    const next = [...drafts];
    const [moved] = next.splice(index, 1);
    next.splice(nextIndex, 0, moved!);
    setDrafts(next);
    const ids = next.map((draft) => draft.id).filter((id): id is string => Boolean(id));
    reorderAssignments.mutate(ids);
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
    saveInheritance.mutate(inheritParent);
  }

  return (
    <FormModal
      open={capabilityKey !== null}
      title={capabilityKey ?? "Capability"}
      titleId={titleId}
      eyebrow="Assignment chain"
      width="wide"
      bodyClassName="llm-assignment-dialog__body"
      contentElement="section"
      onClose={onClose}
      actions={null}
    >
      <section
        className="llm-assignment-dialog__content"
        aria-busy={saveAssignment.isPending}
      >
          {capability ? (
            <div className="llm-assignment-dialog__intro">
              <p>{capability.description}</p>
              <div className="llm-assignment-dialog__chips">
                {capability.required_capabilities.map((tag) => (
                  <Chip key={tag} tone="sky" size="sm">
                    {tag}
                  </Chip>
                ))}
              </div>
              <LlmUsageTotals
                spendUsd={capability.spend_usd_30d}
                calls={capability.calls_30d}
              />
            </div>
          ) : null}
          {err ? (
            <p className="form-error" role="alert">
              {err}
            </p>
          ) : null}
          {hasReadOnlyDefault ? (
            <p className="llm-assignment-dialog__notice">
              Deployment-default rows are synthetic and read-only here. Change
              the provider default or create a direct capability assignment
              instead.
            </p>
          ) : null}
          <div className="llm-assignment-dialog__section-head">
            <h4>Fallback rungs</h4>
            <button type="button" className="btn btn--ghost btn--sm" onClick={addRung}>
              <Plus size={16} aria-hidden="true" /> Add rung
            </button>
          </div>
          {drafts.length ? (
            <ol className="llm-assignment-editor">
              {drafts.map((draft, index) => (
                <li key={draft.key} className="llm-assignment-editor__item">
                  <AssignmentRungForm
                    draft={draft}
                    index={index}
                    indexes={indexes}
                    groups={providerModelGroups}
                    onChange={updateDraft}
                    onSubmit={submitRung}
                    onDelete={(id) => {
                      setClientErr(null);
                      setServerErr(null);
                      deleteAssignment.mutate(id);
                    }}
                    onRemoveDraft={removeDraft}
                    onMove={moveRung}
                    pending={
                      saveAssignment.isPending ||
                      deleteAssignment.isPending ||
                      reorderAssignments.isPending
                    }
                  />
                </li>
              ))}
            </ol>
          ) : (
            <p className="llm-assignment-dialog__empty">
              No direct assignment rungs. Add one or configure inheritance below.
            </p>
          )}
          <form
            className="llm-assignment-dialog__inheritance"
            onSubmit={submitInheritance}
          >
            <div>
              <h4>Inheritance</h4>
              <p>
                {effectiveParent
                  ? `Currently resolves through ${effectiveParent}${explicitParent ? " explicitly" : " by default"}.`
                  : "No parent capability is configured."}
              </p>
              {inheritedChildren.length ? (
                <p>Children: {inheritedChildren.join(", ")}</p>
              ) : null}
            </div>
            <FormModalField label="Parent capability" requirement="optional">
              <select
                value={inheritParent}
                onChange={(event) => setInheritParent(event.target.value)}
              >
                <option value="">No explicit parent</option>
                {parentOptions.map((option) => (
                  <option key={option.key} value={option.key}>
                    {option.key}
                  </option>
                ))}
              </select>
            </FormModalField>
            <div className="llm-assignment-dialog__inherit-actions">
              <button
                type="submit"
                className="btn btn--moss"
                disabled={saveInheritance.isPending || !inheritParent}
              >
                {explicitParent ? "Update inheritance" : "Create inheritance"}
              </button>
              <button
                type="button"
                className="btn btn--ghost"
                disabled={!explicitParent || deleteInheritance.isPending}
                onClick={() => {
                  setClientErr(null);
                  setServerErr(null);
                  deleteInheritance.mutate();
                }}
              >
                Remove inheritance
              </button>
            </div>
          </form>
      </section>
    </FormModal>
  );
}

interface AssignmentRungFormProps {
  draft: DraftRung;
  index: number;
  indexes: LlmIndexes;
  groups: { model: LlmGraphPayload["models"][number]; providerModels: LlmProviderModel[] }[];
  onChange: (key: string, patch: Partial<DraftRung>) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>, draft: DraftRung) => void;
  onDelete: (assignmentId: string) => void;
  onRemoveDraft: (key: string) => void;
  onMove: (index: number, direction: -1 | 1) => void;
  pending: boolean;
}

function AssignmentRungForm(props: AssignmentRungFormProps) {
  // code-health: ignore[nloc] Assignment rung form keeps all fields for one API rung together so save/delete/reorder controls stay auditable.
  const {
    draft,
    index,
    indexes,
    groups,
    onChange,
    onSubmit,
    onDelete,
    onRemoveDraft,
    onMove,
    pending,
  } = props;
  const required = parseRequiredCapabilities(draft.requiredCapabilities) ?? [];
  const selected = indexes.pmById.get(draft.providerModelId);
  const missing = compatibleMissing(selected, required, indexes);
  const readOnly = draft.readOnly;

  return (
    <form className="llm-assignment-editor__form" onSubmit={(event) => onSubmit(event, draft)}>
      <header className="llm-assignment-editor__head">
        <span className="llm-graph-chain__prio">{index === 0 ? "P" : index}</span>
        <strong>{draft.id ? `Rung ${index}` : "New rung"}</strong>
        {readOnly ? (
          <Chip tone="sand" size="sm">
            read-only default
          </Chip>
        ) : null}
        {missing.length ? (
          <Chip tone="rust" size="sm">
            missing {missing.join(", ")}
          </Chip>
        ) : null}
      </header>
      <FormModalField label="Provider-model" requirement="required">
        <select
          value={draft.providerModelId}
          disabled={readOnly}
          aria-invalid={missing.length > 0}
          onChange={(event) =>
            onChange(draft.key, { providerModelId: event.target.value })
          }
        >
          {groups.map(({ model, providerModels }) => (
            <optgroup key={model.id} label={model.display_name}>
              {providerModels.map((pm) => {
                const optionMissing = compatibleMissing(pm, required, indexes);
                return (
                  <option
                    key={pm.id}
                    value={pm.id}
                    disabled={optionMissing.length > 0}
                  >
                    {providerModelLabel(pm, indexes)}
                    {optionMissing.length
                      ? ` - missing ${optionMissing.join(", ")}`
                      : ""}
                  </option>
                );
              })}
            </optgroup>
          ))}
        </select>
      </FormModalField>
      <FormModalGrid>
        <FormModalField label="Max tokens" requirement="optional">
          <input
            type="number"
            min="1"
            value={draft.maxTokens}
            disabled={readOnly}
            onChange={(event) => onChange(draft.key, { maxTokens: event.target.value })}
          />
        </FormModalField>
        <FormModalField label="Temperature" requirement="optional">
          <input
            type="number"
            min="0"
            max="2"
            step="0.1"
            value={draft.temperature}
            disabled={readOnly}
            onChange={(event) =>
              onChange(draft.key, { temperature: event.target.value })
            }
          />
        </FormModalField>
      </FormModalGrid>
      <FormModalField label="Required capabilities" requirement="optional">
        <input
          value={draft.requiredCapabilities}
          disabled={readOnly}
          onChange={(event) =>
            onChange(draft.key, { requiredCapabilities: event.target.value })
          }
        />
      </FormModalField>
      <FormModalField label="Extra API params" requirement="optional">
        <textarea
          rows={3}
          value={draft.extraApiParams}
          disabled={readOnly}
          onChange={(event) =>
            onChange(draft.key, { extraApiParams: event.target.value })
          }
        />
      </FormModalField>
      <label className="llm-registry-form__check">
        <input
          type="checkbox"
          checked={draft.enabled}
          disabled={readOnly}
          onChange={(event) => onChange(draft.key, { enabled: event.target.checked })}
        />
        Enabled
      </label>
      <footer className="llm-assignment-editor__actions">
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          disabled={readOnly || pending}
          onClick={() => onMove(index, -1)}
          aria-label={`Move rung ${index} up`}
        >
          <ArrowUp size={16} aria-hidden="true" />
        </button>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          disabled={readOnly || pending}
          onClick={() => onMove(index, 1)}
          aria-label={`Move rung ${index} down`}
        >
          <ArrowDown size={16} aria-hidden="true" />
        </button>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          disabled={readOnly || pending}
          onClick={() => {
            if (draft.id) {
              onDelete(draft.id);
            } else {
              onRemoveDraft(draft.key);
            }
          }}
        >
          <Trash2 size={16} aria-hidden="true" /> Delete
        </button>
        <button
          type="submit"
          className="btn btn--moss btn--sm"
          disabled={readOnly || pending || missing.length > 0}
        >
          {draft.id ? "Save rung" : "Create rung"}
        </button>
      </footer>
    </form>
  );
}
