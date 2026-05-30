import { type ReactNode, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { type ListEnvelope, unwrapList } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import { workspaceRouteForPathname } from "@/lib/workspaceRoutes";
import {
  InlineNoteField,
  InlineSearchableSelectField,
  InlineSelectField,
  InlineTagPickerField,
  InlineTableForm,
  InlineTextField,
  type InlineTableColumn,
  type InlineTableRow,
} from "@/components/InlineTableForm";
import DeskPage from "@/components/DeskPage";
import DateTime from "@/components/DateTime";
import { Chip, EmptyState, Loading } from "@/components/common";
import { INSTRUCTION_SCOPE_TONE } from "@/lib/tones";
import type { Instruction, Property } from "@/types/api";
import PropertyTabs from "./property/PropertyTabs";

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
  version: number;
  body_md: string;
  created_at: string;
}

interface InstructionEnvelope {
  instruction: InstructionMeta;
  current_revision: InstructionRevision;
}

interface AreaOption {
  id: string;
  name: string;
}

interface InstructionDraft {
  title: string;
  body_md: string;
  scope: InstructionScope;
  property_id: string | null;
  property_ids: string[];
  area_id: string | null;
  area_label: string | null;
  tags: string[];
}

interface InstructionSave {
  rowId: string;
  instructionId: string;
  draft: InstructionDraft;
}

const CREATE_ROW_ID = "__new_instruction__";

const SCOPE_OPTIONS = [
  { value: "global", label: "Workspace" },
  { value: "property", label: "Property" },
  { value: "area", label: "Area" },
];

const EMPTY_CREATE_DRAFT: InstructionDraft = {
  title: "",
  body_md: "",
  scope: "global",
  property_id: null,
  property_ids: [],
  area_id: null,
  area_label: null,
  tags: [],
};

function preview(body: string): string {
  return body.length > 180 ? body.slice(0, 180) + "..." : body;
}

function slugFromTitle(title: string): string {
  return title
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "") || "instruction";
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

function draftFromInstruction(instruction: Instruction): InstructionDraft {
  const propertyIds = instructionPropertyIds(instruction);
  return {
    title: instruction.title,
    body_md: instruction.body_md,
    scope: instruction.scope,
    property_id: instruction.property_id ?? propertyIds[0] ?? null,
    property_ids: propertyIds,
    area_id: instruction.area_id ?? instruction.area,
    area_label: instruction.area,
    tags: instruction.tags,
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

function validateDraft(draft: InstructionDraft): ReactNode {
  if (!draft.title.trim()) return "Add a title.";
  if (!draft.body_md.trim()) return "Add markdown body text.";
  if (draft.scope === "property" && draft.property_ids.length === 0) {
    return "Select at least one property.";
  }
  if (draft.scope === "area" && !draft.property_id) return "Select one property before choosing an area.";
  if (draft.scope === "area" && !draft.area_id) return "Select one area.";
  return null;
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

function instructionBody(draft: InstructionDraft) {
  return {
    title: draft.title.trim(),
    body_md: draft.body_md,
    scope: draft.scope,
    property_id:
      draft.scope === "global" ? null :
      draft.scope === "area" ? draft.property_id :
      draft.property_ids[0] ?? null,
    property_ids: draft.scope === "property" ? draft.property_ids : undefined,
    area_id: draft.scope === "area" ? draft.area_id : null,
    tags: normalizedTags(draft.tags),
    change_note: null,
  };
}

function nextScopeDraft(draft: InstructionDraft, scope: InstructionScope): InstructionDraft {
  const fallbackPropertyId = draft.property_ids[0] ?? draft.property_id;
  if (scope === "global") {
    return { ...draft, scope, property_id: null, property_ids: [], area_id: null, area_label: null };
  }
  if (scope === "property") {
    const propertyIds = draft.property_ids.length > 0
      ? draft.property_ids
      : fallbackPropertyId
        ? [fallbackPropertyId]
        : [];
    return {
      ...draft,
      scope,
      property_id: propertyIds[0] ?? null,
      property_ids: propertyIds,
      area_id: null,
      area_label: null,
    };
  }
  return {
    ...draft,
    scope,
    property_id: fallbackPropertyId ?? null,
    property_ids: [],
    area_id: null,
    area_label: null,
  };
}

function instructionsListUrl(propertyId: string | undefined): string {
  if (!propertyId) return "/api/v1/instructions";
  const params = new URLSearchParams({ property_id: propertyId });
  return `/api/v1/instructions?${params.toString()}`;
}

export default function InstructionsPage() {
  const { pathname } = useLocation();
  const { pid } = useParams<{ pid?: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [rowEdits, setRowEdits] = useState<ReadonlyMap<string, InlineTableRow<InstructionDraft>>>(new Map());
  const [createRow, setCreateRow] = useState<InlineTableRow<InstructionDraft>>(() => newCreateRow());

  const instrQ = useQuery({
    queryKey: qk.instructionsList(pid),
    queryFn: () => fetchJson<ListEnvelope<Instruction>>(instructionsListUrl(pid)).then(unwrapList),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  const activeAreaPropertyId = useMemo(() => {
    if (createRow.draft.scope === "area") return createRow.draft.property_id;
    for (const row of rowEdits.values()) {
      if (row.draft.scope === "area") return row.draft.property_id;
    }
    return null;
  }, [createRow.draft.property_id, createRow.draft.scope, rowEdits]);

  const areasQ = useQuery({
    queryKey: qk.propertyAreas(activeAreaPropertyId ?? ""),
    queryFn: () =>
      fetchJson<ListEnvelope<AreaOption>>(
        "/api/v1/properties/" + activeAreaPropertyId + "/areas",
      ).then(unwrapList),
    enabled: Boolean(activeAreaPropertyId),
  });

  const create = useMutation({
    mutationFn: (draft: InstructionDraft) =>
      fetchJson<InstructionEnvelope>("/api/v1/instructions", {
        method: "POST",
        body: {
          slug: slugFromTitle(draft.title),
          ...instructionBody(draft),
        },
      }).then(toInstruction),
    onSuccess: (instruction) => {
      queryClient.setQueryData(qk.instruction(instruction.id), instruction);
      void queryClient.invalidateQueries({ queryKey: qk.instructions() });
      void queryClient.invalidateQueries({ queryKey: qk.instruction(instruction.id) });
      setCreateRow(newCreateRow());
      navigate(workspaceRouteForPathname(pathname, "/instructions/" + instruction.id));
    },
    onError: () => {
      setCreateRow((row) => ({
        ...row,
        saving: false,
        error: "Failed to create instruction.",
      }));
    },
  });

  const update = useMutation({
    mutationFn: ({ instructionId, draft }: InstructionSave) =>
      fetchJson<InstructionEnvelope>("/api/v1/instructions/" + instructionId, {
        method: "PATCH",
        body: instructionBody(draft),
      }).then(toInstruction),
    onSuccess: (instruction, vars) => {
      queryClient.setQueryData(qk.instruction(instruction.id), instruction);
      void queryClient.invalidateQueries({ queryKey: qk.instructions() });
      void queryClient.invalidateQueries({ queryKey: qk.instruction(instruction.id) });
      void queryClient.invalidateQueries({ queryKey: qk.instructionVersions(instruction.id) });
      setRowEdits((current) => {
        const next = new Map(current);
        next.delete(vars.rowId);
        return next;
      });
    },
    onError: (_error, vars) => {
      setRowEdits((current) => patchRows(current, vars.rowId, (row) => ({
        ...row,
        saving: false,
        error: "Failed to save instruction.",
      })));
    },
  });

  const sub = "The workspace knowledge base. Workspace rules, property quirks, and area-specific tips. Staff see the ones that apply to their task.";

  if (instrQ.isPending || propsQ.isPending) {
    return (
      <DeskPage title="Instructions" sub={sub}>
        <Loading />
      </DeskPage>
    );
  }
  if (!instrQ.data || !propsQ.data) {
    return (
      <DeskPage title="Instructions" sub={sub}>
        Failed to load.
      </DeskPage>
    );
  }

  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));
  const propertyOptions = propsQ.data.map((property) => ({
    value: property.id,
    label: property.name,
  }));
  const searchablePropertyOptions = propsQ.data.map(propertySelectOption);
  const tagOptions = Array.from(new Set(instrQ.data.flatMap((instruction) => instruction.tags)))
    .sort((left, right) => left.localeCompare(right))
    .map((tag) => ({ value: tag, label: "#" + tag }));
  const countBy = (scope: InstructionScope): number =>
    instrQ.data.filter((i) => i.scope === scope).length;
  const rows = instrQ.data.map((instruction) => {
    const base = rowFromInstruction(instruction);
    const local = rowEdits.get(instruction.id);
    return local ? { ...base, ...local, draft: local.draft } : base;
  });
  const columns = instructionColumns({
    pathname,
    propsById,
    propertyOptions,
    searchablePropertyOptions,
    areaOptions: (areasQ.data ?? []).map(areaSelectOption),
    areasPending: areasQ.isPending,
    tagOptions,
  });

  return (
    <DeskPage title="Instructions" sub={sub}>
      {pid ? (
        <PropertyTabs
          pathname={pathname}
          propertyId={pid}
          activeRelatedPage="instructions"
        />
      ) : null}

      <section className="panel">
        <div className="desk-filters">
          <span className="chip chip--ghost chip--sm chip--active">All</span>
          <span className="chip chip--ghost chip--sm">Workspace · {countBy("global")}</span>
          <span className="chip chip--ghost chip--sm">Property · {countBy("property")}</span>
          <span className="chip chip--ghost chip--sm">Area · {countBy("area")}</span>
        </div>

        <InlineTableForm
          ariaLabel="Instructions"
          className="instructions-inline-table"
          columns={columns}
          rows={rows}
          trailingCreateRow={createRow}
          saveMode="explicit"
          createRowLabel="New instruction"
          actionDisplay="icons"
          onDraftChange={(rowId, patch) => {
            if (rowId === CREATE_ROW_ID) {
              setCreateRow((row) => patchInstructionRow(row, patch));
              return;
            }
            setRowEdits((current) => patchRows(current, rowId, (row) => patchInstructionRow(row, patch)));
          }}
          onEdit={(rowId) => {
            const instruction = instrQ.data.find((candidate) => candidate.id === rowId);
            if (!instruction) return;
            setRowEdits(new Map([[rowId, { ...rowFromInstruction(instruction), editing: true }]]));
          }}
          onSave={(rowId) => {
            if (rowId === CREATE_ROW_ID) {
              const validation = validateDraft(createRow.draft);
              if (validation) {
                setCreateRow((row) => ({ ...row, dirty: true, validation }));
                return;
              }
              setCreateRow((row) => ({ ...row, saving: true, error: undefined }));
              create.mutate(createRow.draft);
              return;
            }
            const row = rowEdits.get(rowId);
            if (!row) return;
            const validation = validateDraft(row.draft);
            if (validation) {
              setRowEdits((current) => patchRows(current, rowId, (currentRow) => ({
                ...currentRow,
                validation,
              })));
              return;
            }
            setRowEdits((current) => patchRows(current, rowId, (currentRow) => ({
              ...currentRow,
              saving: true,
              error: undefined,
            })));
            update.mutate({ rowId, instructionId: rowId, draft: row.draft });
          }}
          onCancel={(rowId) => {
            if (rowId === CREATE_ROW_ID) {
              setCreateRow(newCreateRow());
              return;
            }
            setRowEdits((current) => {
              const next = new Map(current);
              next.delete(rowId);
              return next;
            });
          }}
          renderDetail={({ row, update: patch, disabled }) => (
            row.editing ? (
              <div className="instruction-inline-detail">
                <InlineNoteField
                  value={row.draft.body_md}
                  disabled={disabled}
                  ariaLabel={`Markdown for ${row.label ?? "instruction"}`}
                  placeholder="Markdown body"
                  onChange={(body_md) => patch({ body_md })}
                />
              </div>
            ) : null
          )}
          getRowLabel={(row) => row.label ?? "New instruction"}
          emptyState={
            <EmptyState
              variant="compact"
              title="No instructions yet"
              copy="Add the first workspace, property, or area instruction in the row below."
            />
          }
        />
      </section>
    </DeskPage>
  );
}

function instructionColumns({
  pathname,
  propsById,
  propertyOptions,
  searchablePropertyOptions,
  areaOptions,
  areasPending,
  tagOptions,
}: {
  pathname: string;
  propsById: ReadonlyMap<string, Property>;
  propertyOptions: readonly { value: string; label: string }[];
  searchablePropertyOptions: readonly ReturnType<typeof propertySelectOption>[];
  areaOptions: readonly ReturnType<typeof areaSelectOption>[];
  areasPending: boolean;
  tagOptions: readonly { value: string; label: string }[];
}): InlineTableColumn<InstructionDraft>[] {
  return [
    {
      key: "title",
      header: "Instruction",
      width: { flex: 1.7, min: 220 },
      renderRead: ({ row }) => (
        <Link
          to={workspaceRouteForPathname(pathname, "/instructions/" + row.id)}
          className="instruction-inline-title"
        >
          <strong>{row.draft.title}</strong>
          <span>{preview(row.draft.body_md)}</span>
        </Link>
      ),
      renderEdit: ({ row, update, disabled }) => (
        <InlineTextField
          value={row.draft.title}
          disabled={disabled}
          ariaLabel="Instruction title"
          placeholder="Instruction title"
          onChange={(title) => update({ title })}
        />
      ),
    },
    {
      key: "scope",
      header: "Scope",
      width: { flex: 0.8, min: 150 },
      renderRead: ({ row }) => (
        <Chip tone={INSTRUCTION_SCOPE_TONE[row.draft.scope]} size="sm">
          {scopeLabel(row.draft.scope)}
        </Chip>
      ),
      renderEdit: ({ row, update, disabled }) => (
        <InlineSelectField
          value={row.draft.scope}
          options={SCOPE_OPTIONS}
          disabled={disabled}
          ariaLabel="Instruction scope"
          onChange={(scope) => update(nextScopeDraft(row.draft, scope as InstructionScope))}
        />
      ),
    },
    {
      key: "applies",
      header: "Applies to",
      width: { flex: 1.45, min: 240 },
      renderRead: ({ row }) => (
        <InstructionScopeTargets draft={row.draft} propsById={propsById} />
      ),
      renderEdit: ({ row, update, disabled }) => {
        if (row.draft.scope === "global") {
          return <span className="muted">Every property and task</span>;
        }
        if (row.draft.scope === "area") {
          return (
            <div className="instruction-inline-scope-fields">
              <InlineSearchableSelectField
                value={row.draft.property_id ?? ""}
                options={searchablePropertyOptions}
                disabled={disabled}
                ariaLabel="Instruction property"
                blankOption={{ label: "Select property" }}
                onChange={(propertyId) => update({
                  property_id: propertyId || null,
                  property_ids: [],
                  area_id: null,
                  area_label: null,
                })}
              />
              <InlineSearchableSelectField
                value={row.draft.area_id ?? ""}
                options={areaOptions}
                disabled={disabled || !row.draft.property_id || areasPending}
                ariaLabel="Instruction area"
                blankOption={{ label: row.draft.property_id ? "Select area" : "Select property first" }}
                noResultsLabel="No areas"
                renderOptionSecondaryText={() => null}
                onChange={(areaId) => update({
                  area_id: areaId || null,
                  area_label: areaOptions.find((area) => area.value === areaId)?.label ?? null,
                })}
              />
            </div>
          );
        }
        return (
          <InlineTagPickerField
            value={row.draft.property_ids}
            options={propertyOptions}
            disabled={disabled}
            searchable
            ariaLabel="Instruction properties"
            inputLabel="Filter properties"
            placeholder="Find property..."
            onChange={(property_ids) => update({
              property_ids,
              property_id: property_ids[0] ?? null,
              area_id: null,
              area_label: null,
            })}
          />
        );
      },
    },
    {
      key: "tags",
      header: "Tags",
      width: { flex: 1.1, min: 210 },
      renderRead: ({ row }) => (
        <InstructionTags tags={row.draft.tags} />
      ),
      renderEdit: ({ row, update, disabled }) => (
        <InlineTagPickerField
          value={row.draft.tags}
          options={tagOptions}
          disabled={disabled}
          searchable
          allowCustomValues
          ariaLabel="Instruction tags"
          inputLabel="Add instruction tag"
          placeholder="Add tag..."
          normalizeCustomValue={(tag) => tag.trim().toLocaleLowerCase()}
          onChange={(tags) => update({ tags: normalizedTags(tags) })}
        />
      ),
    },
    {
      key: "updated",
      header: "Updated",
      width: { flex: 0.75, min: 145 },
      renderRead: ({ row }) => (
        row.id === CREATE_ROW_ID ? <span className="muted">Draft</span> : row.meta
      ),
      renderEdit: ({ row }) => (
        row.id === CREATE_ROW_ID ? <span className="muted">Draft</span> : row.meta
      ),
    },
  ];
}

function rowFromInstruction(instruction: Instruction): InlineTableRow<InstructionDraft> {
  return {
    id: instruction.id,
    label: instruction.title,
    draft: draftFromInstruction(instruction),
    editing: false,
    dirty: false,
    meta: (
      <span className="mono muted">
        v{instruction.version} · <DateTime value={instruction.updated_at} />
      </span>
    ),
  };
}

function newCreateRow(): InlineTableRow<InstructionDraft> {
  return {
    id: CREATE_ROW_ID,
    label: "New instruction",
    draft: EMPTY_CREATE_DRAFT,
    editing: true,
    dirty: false,
    isNew: true,
  };
}

function patchInstructionRow(
  row: InlineTableRow<InstructionDraft>,
  patch: Partial<InstructionDraft>,
): InlineTableRow<InstructionDraft> {
  return {
    ...row,
    draft: { ...row.draft, ...patch },
    dirty: true,
    error: undefined,
    validation: undefined,
  };
}

function patchRows(
  rows: ReadonlyMap<string, InlineTableRow<InstructionDraft>>,
  rowId: string,
  patch: (row: InlineTableRow<InstructionDraft>) => InlineTableRow<InstructionDraft>,
): ReadonlyMap<string, InlineTableRow<InstructionDraft>> {
  const row = rows.get(rowId);
  if (!row) return rows;
  const next = new Map(rows);
  next.set(rowId, patch(row));
  return next;
}

function scopeLabel(scope: InstructionScope): string {
  if (scope === "global") return "Workspace";
  if (scope === "property") return "Property";
  return "Area";
}

function InstructionScopeTargets({
  draft,
  propsById,
}: {
  draft: InstructionDraft;
  propsById: ReadonlyMap<string, Property>;
}) {
  if (draft.scope === "global") {
    return <Chip tone="ghost" size="sm">Workspace</Chip>;
  }
  if (draft.scope === "area") {
    const propName = draft.property_id ? propsById.get(draft.property_id)?.name ?? "Property" : "Property";
    return (
      <span className="instruction-inline-chips">
        <Chip tone="ghost" size="sm">{propName}</Chip>
        <Chip tone="ghost" size="sm">{draft.area_label ?? draft.area_id ?? "Area"}</Chip>
      </span>
    );
  }
  return (
    <span className="instruction-inline-chips">
      {draft.property_ids.map((propertyId) => (
        <Chip key={propertyId} tone="ghost" size="sm">
          {propsById.get(propertyId)?.name ?? propertyId}
        </Chip>
      ))}
    </span>
  );
}

function InstructionTags({ tags }: { tags: readonly string[] }) {
  if (tags.length === 0) return <span className="muted">No tags</span>;
  return (
    <span className="instruction-inline-chips">
      {tags.map((tag) => (
        <Chip key={tag} tone="ghost" size="sm">#{tag}</Chip>
      ))}
    </span>
  );
}
