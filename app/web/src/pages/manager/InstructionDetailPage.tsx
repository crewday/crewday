import { Fragment, type FormEvent, type ReactNode, useCallback, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { type ListEnvelope, unwrapList } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import { workspaceRouteForPathname } from "@/lib/workspaceRoutes";
import { useModalDialog } from "@/lib/modalDialog";
import {
  InlineNoteField,
  InlineSearchableSelectField,
  InlineSelectField,
  InlineTagPickerField,
  InlineTextField,
} from "@/components/InlineTableForm";
import DeskPage from "@/components/DeskPage";
import DateTime from "@/components/DateTime";
import { Chip, Loading } from "@/components/common";
import { INSTRUCTION_SCOPE_TONE } from "@/lib/tones";
import type { Instruction, Property } from "@/types/api";

type InstructionScope = Instruction["scope"];

interface InstructionMeta {
  id: string;
  title: string;
  scope: InstructionScope;
  property_id: string | null;
  property_ids: string[];
  area_id: string | null;
  tags: string[];
}

interface InstructionRevision {
  id: string;
  instruction_id: string;
  version: number;
  body_md: string;
  change_note: string | null;
  created_at: string;
}

interface AreaOption {
  id: string;
  name: string;
}

interface InstructionEnvelope {
  instruction: InstructionMeta;
  current_revision: InstructionRevision;
}

interface InstructionPatch {
  title: string;
  body_md: string;
  scope: InstructionScope;
  property_id: string | null;
  property_ids: string[];
  area_id: string | null;
  tags: string[];
  change_note: string;
}

const EMPTY_PATCH: InstructionPatch = {
  title: "",
  body_md: "",
  scope: "global",
  property_id: null,
  property_ids: [],
  area_id: null,
  tags: [],
  change_note: "",
};

const SCOPE_OPTIONS = [
  { value: "global", label: "Workspace" },
  { value: "property", label: "Property" },
  { value: "area", label: "Area" },
];

// Mock body is plain text with newlines; render with <br> between lines.
// Real Markdown rendering will land when the spec calls for it.
function renderBody(body: string): ReactNode {
  const lines = body.split("\n");
  return lines.map((line, idx) => (
    <Fragment key={idx}>
      {line}
      {idx < lines.length - 1 && <br />}
    </Fragment>
  ));
}

function toInstruction(envelope: InstructionEnvelope): Instruction {
  return {
    id: envelope.instruction.id,
    title: envelope.instruction.title,
    scope: envelope.instruction.scope,
    property_id: envelope.instruction.property_id,
    property_ids: envelope.instruction.property_ids,
    area_id: envelope.instruction.area_id,
    area: envelope.instruction.area_id,
    tags: envelope.instruction.tags,
    body_md: envelope.current_revision.body_md,
    version: envelope.current_revision.version,
    updated_at: envelope.current_revision.created_at,
  };
}

function toPatch(i: Instruction): InstructionPatch {
  const propertyIds = instructionPropertyIds(i);
  return {
    title: i.title,
    body_md: i.body_md,
    scope: i.scope,
    property_id: i.property_id ?? propertyIds[0] ?? null,
    property_ids: propertyIds,
    area_id: i.area,
    tags: i.tags,
    change_note: "",
  };
}

function instructionPropertyIds(instruction: Instruction): string[] {
  return instruction.property_ids && instruction.property_ids.length > 0
    ? instruction.property_ids
    : instruction.property_id
      ? [instruction.property_id]
      : [];
}

function normalizedTags(tags: readonly string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const rawTag of tags) {
    const tag = rawTag.trim().toLocaleLowerCase();
    if (!tag || seen.has(tag)) continue;
    seen.add(tag);
    out.push(tag);
  }
  return out;
}

function canSubmitPatch(patch: InstructionPatch): boolean {
  if (!patch.title.trim()) return false;
  if (patch.scope === "property") return patch.property_ids.length > 0;
  if (patch.scope === "area") return Boolean(patch.property_id && patch.area_id);
  return true;
}

function instructionEditAction(onEdit: () => void): ReactNode {
  return (
    <button type="button" className="btn btn--moss" onClick={onEdit}>
      Edit
    </button>
  );
}

function instructionDetailSub({
  contextPropertyId,
  pathname,
  scope,
  scopeSummary,
}: {
  contextPropertyId: string | null | undefined;
  pathname: string;
  scope: InstructionScope;
  scopeSummary: string;
}): ReactNode {
  return (
    <>
      <Link
        to={contextPropertyId
          ? workspaceRouteForPathname(pathname, "/property/" + contextPropertyId + "/instructions")
          : workspaceRouteForPathname(pathname, "/instructions")}
        className="link"
      >
        ← All instructions
      </Link>{" "}·{" "}
      <Chip tone={INSTRUCTION_SCOPE_TONE[scope]} size="sm">{scopeSummary}</Chip>
    </>
  );
}

function nextScopePatch(patch: InstructionPatch, scope: InstructionScope): InstructionPatch {
  const fallbackPropertyId = patch.property_ids[0] ?? patch.property_id;
  if (scope === "global") {
    return { ...patch, scope, property_id: null, property_ids: [], area_id: null };
  }
  if (scope === "property") {
    const propertyIds = patch.property_ids.length > 0
      ? patch.property_ids
      : fallbackPropertyId
        ? [fallbackPropertyId]
        : [];
    return {
      ...patch,
      scope,
      property_id: propertyIds[0] ?? null,
      property_ids: propertyIds,
      area_id: null,
    };
  }
  return {
    ...patch,
    scope,
    property_id: fallbackPropertyId ?? null,
    property_ids: [],
    area_id: null,
  };
}

function propertySelectOption(property: Property) {
  return {
    value: property.id,
    label: property.name,
    secondaryText: property.city,
    searchText: `${property.name} ${property.city} ${property.timezone}`,
  };
}

function areaSelectOption(area: AreaOption) {
  return { value: area.id, label: area.name };
}

function patchBody(patch: InstructionPatch) {
  return {
    title: patch.title,
    body_md: patch.body_md,
    scope: patch.scope,
    property_id:
      patch.scope === "global" ? null :
      patch.scope === "area" ? patch.property_id :
      patch.property_ids[0] ?? null,
    property_ids: patch.scope === "property" ? patch.property_ids : undefined,
    area_id: patch.scope === "area" ? patch.area_id : null,
    tags: normalizedTags(patch.tags),
    change_note: patch.change_note || null,
  };
}

// react-doctor-disable-next-line react-doctor/no-giant-component -- Existing promoted surface is intentionally deferred until a focused component split preserves behavior.
export default function InstructionDetailPage() {
  // code-health: ignore[ccn] Instruction detail route coordinates read/ack/comment mutations around one promoted detail layout.
  const { iid } = useParams<{ iid: string }>();
  const { pathname, search } = useLocation();
  const contextPropertyId = new URLSearchParams(search).get("property_id") ?? undefined;
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [draft, setDraft] = useState<InstructionPatch>(EMPTY_PATCH);
  const closeVersions = useCallback(() => setVersionsOpen(false), []);

  const instrQ = useQuery({
    queryKey: qk.instruction(iid ?? ""),
    queryFn: () => fetchJson<InstructionEnvelope>("/api/v1/instructions/" + iid).then(toInstruction),
    enabled: Boolean(iid),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const instrListQ = useQuery({
    queryKey: qk.instructionsList(null),
    queryFn: () => fetchJson<ListEnvelope<Instruction>>("/api/v1/instructions").then(unwrapList),
    enabled: editing,
  });
  const areasPropertyId = editing ? draft.property_id : instrQ.data?.property_id ?? null;
  const areasQ = useQuery({
    queryKey: qk.propertyAreas(areasPropertyId ?? ""),
    queryFn: () =>
      fetchJson<ListEnvelope<AreaOption>>(
        "/api/v1/properties/" + areasPropertyId + "/areas",
      ).then(unwrapList),
    enabled: Boolean(areasPropertyId && (editing || instrQ.data?.scope === "area")),
  });
  const versionsQ = useQuery({
    queryKey: qk.instructionVersions(iid ?? ""),
    queryFn: () =>
      fetchJson<ListEnvelope<InstructionRevision>>(
        "/api/v1/instructions/" + iid + "/versions",
      ).then(unwrapList),
    enabled: Boolean(iid && versionsOpen),
  });
  const save = useMutation({
    mutationFn: (patch: InstructionPatch) =>
      fetchJson<InstructionEnvelope>("/api/v1/instructions/" + iid, {
        method: "PATCH",
        body: patchBody(patch),
      }).then(toInstruction),
    onSuccess: (next) => {
      queryClient.setQueryData(qk.instruction(next.id), next);
      void queryClient.invalidateQueries({ queryKey: qk.instructions() });
      void queryClient.invalidateQueries({ queryKey: qk.instruction(next.id) });
      void queryClient.invalidateQueries({ queryKey: qk.instructionVersions(next.id) });
      setEditing(false);
    },
  });

  const versionsDialog = useModalDialog(closeVersions);

  if (!iid) return <DeskPage title="Instruction">Missing instruction id.</DeskPage>;
  if (instrQ.isPending || propsQ.isPending) {
    return <DeskPage title="Instruction"><Loading /></DeskPage>;
  }
  if (!instrQ.data || !propsQ.data) {
    return <DeskPage title="Instruction">Failed to load.</DeskPage>;
  }

  const i = instrQ.data;
  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));
  const propertyIds = instructionPropertyIds(i);
  const propName = i.property_id ? propsById.get(i.property_id)?.name ?? "" : "";
  const areaName =
    i.scope === "area"
      ? areasQ.data?.find((area) => area.id === i.area)?.name ?? i.area ?? ""
      : "";
  const scopeSummary =
    i.scope === "global" ? "Workspace" :
    i.scope === "property" ? propertyScopeSummary(propertyIds, propsById) :
    propName + (areaName ? " · " + areaName : "");
  const propertyOptions = propsQ.data.map((property) => ({
    value: property.id,
    label: property.name,
  }));
  const searchablePropertyOptions = propsQ.data.map(propertySelectOption);
  const tagOptions = Array.from(new Set([
    ...(instrListQ.data ?? []).flatMap((instruction) => instruction.tags),
    ...i.tags,
  ]))
    .sort((left, right) => left.localeCompare(right))
    .map((tag) => ({ value: tag, label: "#" + tag }));

  const sub = instructionDetailSub({
    contextPropertyId,
    pathname,
    scope: i.scope,
    scopeSummary,
  });
  const actions = instructionEditAction(() => {
    setDraft(toPatch(i));
    setEditing(true);
  });
  const overflow = [
    { label: "View revisions", onSelect: () => setVersionsOpen(true) },
  ];

  function submitEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmitPatch(draft)) return;
    save.mutate(draft);
  }

  function closeEdit() {
    setEditing(false);
  }

  return (
    <DeskPage
      title={i.title}
      sub={sub}
      actions={actions}
      overflow={overflow}
    >
      {editing ? (
        <form className="panel instruction-detail-editor" onSubmit={submitEdit}>
          <header className="panel__head">
            <h2>Edit instruction</h2>
            <div className="btn-group">
              <button type="button" className="btn btn--ghost" onClick={closeEdit}>
                Cancel
              </button>
              <button
                type="submit"
                className="btn btn--moss"
                disabled={save.isPending || !canSubmitPatch(draft)}
              >
                Save
              </button>
            </div>
          </header>
          <div className="instruction-detail-editor__grid">
            <div className="instruction-detail-editor__field">
              <span>Title</span>
              <InlineTextField
                value={draft.title}
                ariaLabel="Title"
                onChange={(title) => setDraft({ ...draft, title })}
              />
            </div>
            <div className="instruction-detail-editor__field">
              <span>Scope</span>
              <InlineSelectField
                value={draft.scope}
                options={SCOPE_OPTIONS}
                ariaLabel="Scope"
                onChange={(scope) => setDraft(nextScopePatch(draft, scope as InstructionScope))}
              />
            </div>
          </div>
          {draft.scope === "property" ? (
            <div className="instruction-detail-editor__field">
              <span>Properties</span>
              <InlineTagPickerField
                value={draft.property_ids}
                options={propertyOptions}
                searchable
                ariaLabel="Instruction properties"
                inputLabel="Filter properties"
                placeholder="Find property..."
                onChange={(property_ids) => setDraft({
                  ...draft,
                  property_ids,
                  property_id: property_ids[0] ?? null,
                  area_id: null,
                })}
              />
            </div>
          ) : null}
          {draft.scope === "area" ? (
            <div className="instruction-detail-editor__grid">
              <div className="instruction-detail-editor__field">
                <span>Property</span>
                <InlineSearchableSelectField
                  value={draft.property_id ?? ""}
                  options={searchablePropertyOptions}
                  ariaLabel="Property"
                  blankOption={{ label: "Select property" }}
                  onChange={(propertyId) => setDraft({
                    ...draft,
                    property_id: propertyId || null,
                    property_ids: [],
                    area_id: null,
                  })}
                />
              </div>
              <div className="instruction-detail-editor__field">
                <span>Area</span>
                <InlineSearchableSelectField
                  value={draft.area_id ?? ""}
                  options={(areasQ.data ?? []).map(areaSelectOption)}
                  disabled={!draft.property_id || areasQ.isPending}
                  ariaLabel="Area"
                  blankOption={{ label: draft.property_id ? "Select area" : "Select property first" }}
                  noResultsLabel="No areas"
                  renderOptionSecondaryText={() => null}
                  onChange={(areaId) => setDraft({ ...draft, area_id: areaId || null })}
                />
              </div>
            </div>
          ) : null}
          <div className="instruction-detail-editor__field">
            <span>Markdown</span>
            <InlineNoteField
              value={draft.body_md}
              ariaLabel="Markdown"
              onChange={(body_md) => setDraft({ ...draft, body_md })}
            />
          </div>
          <div className="instruction-detail-editor__field">
            <span>Tags</span>
            <InlineTagPickerField
              value={draft.tags}
              options={tagOptions}
              searchable
              allowCustomValues
              ariaLabel="Instruction tags"
              inputLabel="Add instruction tag"
              placeholder="Add tag..."
              normalizeCustomValue={(tag) => tag.trim().toLocaleLowerCase()}
              onChange={(tags) => setDraft({ ...draft, tags: normalizedTags(tags) })}
            />
          </div>
          <div className="instruction-detail-editor__field">
            <span>Change note</span>
            <InlineTextField
              value={draft.change_note}
              ariaLabel="Change note"
              onChange={(change_note) => setDraft({ ...draft, change_note })}
            />
          </div>
          {save.isError && <p className="form-error">Failed to save.</p>}
        </form>
      ) : null}

      <article className="panel panel--article">
        <div className="kb-body">
          {/* react-doctor-disable-next-line react-doctor/no-render-in-render -- Markdown body rendering is a pure parser formatter shared by the article and history drawer. */}
          {renderBody(i.body_md)}
        </div>
        <footer className="kb-footer">
          <div>
            {i.tags.map((t) => (
              <Chip key={t} tone="ghost" size="sm">#{t}</Chip>
            ))}
          </div>
          <div className="mono muted">
            Revision {i.version} · saved <DateTime value={i.updated_at} showTime />
          </div>
        </footer>
      </article>

      <section className="panel">
        <header className="panel__head"><h2>Where this applies</h2></header>
        <ul className="task-list task-list--desk">
          <li className="task-row">
            <span className="task-row__time mono">via scope</span>
            <span className="task-row__title">
              <strong>All tasks matching the scope above</strong>
              <span className="instruction-inline-chips">
                {i.scope === "global" ? <Chip tone="ghost" size="sm">Workspace</Chip> : null}
                {i.scope === "property" ? propertyIds.map((propertyId) => (
                  <Chip key={propertyId} tone="ghost" size="sm">
                    {propsById.get(propertyId)?.name ?? propertyId}
                  </Chip>
                )) : null}
                {i.scope === "area" ? (
                  <>
                    <Chip tone="ghost" size="sm">{propName || "Property"}</Chip>
                    <Chip tone="ghost" size="sm">{areaName || i.area || "Area"}</Chip>
                  </>
                ) : null}
              </span>
            </span>
            <Chip tone="ghost" size="sm">automatic</Chip>
          </li>
          <li className="task-row">
            <span className="task-row__time mono">linked to template</span>
            <span className="task-row__title"><strong>Linen change, master bedroom</strong></span>
            <Chip tone="moss" size="sm">template link</Chip>
          </li>
        </ul>
      </section>

      {versionsOpen && (
        <dialog
          ref={versionsDialog.ref}
          className="day-drawer"
          aria-label="Instruction history"
          onCancel={versionsDialog.onCancel}
        >
          <header className="day-drawer__head">
            <div>
              <div className="day-drawer__eyebrow">Instruction history</div>
              <h2 className="day-drawer__title">{i.title}</h2>
            </div>
            <button
              type="button"
              className="day-drawer__close"
              onClick={closeVersions}
              aria-label="Close instruction history"
            >
              ×
            </button>
          </header>
          <div className="day-drawer__body">
            {versionsQ.isPending ? <Loading /> : null}
            {versionsQ.isError ? <p>Failed to load.</p> : null}
            {versionsQ.data?.map((version) => (
              <section key={version.id} className="day-drawer__section">
                <h3 className="day-drawer__section-title">
                  Revision {version.version} · <DateTime value={version.created_at} showTime />
                </h3>
                {version.change_note && <p className="day-drawer__muted">{version.change_note}</p>}
                <div className="kb-body">
                  {/* react-doctor-disable-next-line react-doctor/no-render-in-render -- Markdown body rendering is a pure parser formatter shared by the article and history drawer. */}
                  {renderBody(version.body_md)}
                </div>
              </section>
            ))}
          </div>
        </dialog>
      )}
    </DeskPage>
  );
}

function propertyScopeSummary(
  propertyIds: readonly string[],
  propsById: ReadonlyMap<string, Property>,
) {
  if (propertyIds.length === 0) return "Property";
  if (propertyIds.length === 1) return propsById.get(propertyIds[0] ?? "")?.name ?? "Property";
  return `${propertyIds.length} properties`;
}
