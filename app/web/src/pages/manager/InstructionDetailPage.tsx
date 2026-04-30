import { Fragment, type FormEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { type ListEnvelope, unwrapList } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { INSTRUCTION_SCOPE_TONE } from "@/lib/tones";
import type { Instruction, Property } from "@/types/api";

interface InstructionMeta {
  id: string;
  title: string;
  scope: Instruction["scope"];
  property_id: string | null;
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
  scope: Instruction["scope"];
  property_id: string | null;
  area_id: string | null;
  tags: string[];
  change_note: string;
}

const EMPTY_PATCH: InstructionPatch = {
  title: "",
  body_md: "",
  scope: "global",
  property_id: null,
  area_id: null,
  tags: [],
  change_note: "",
};

function fmtSaved(iso: string): string {
  return new Date(iso).toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

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
    area: envelope.instruction.area_id,
    tags: envelope.instruction.tags,
    body_md: envelope.current_revision.body_md,
    version: envelope.current_revision.version,
    updated_at: envelope.current_revision.created_at,
  };
}

function toPatch(i: Instruction): InstructionPatch {
  return {
    title: i.title,
    body_md: i.body_md,
    scope: i.scope,
    property_id: i.property_id,
    area_id: i.area,
    tags: i.tags,
    change_note: "",
  };
}

function parseTags(raw: string): string[] {
  return raw
    .split(",")
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function canSubmitPatch(patch: InstructionPatch): boolean {
  if (!patch.title.trim()) return false;
  if (patch.scope === "property") return Boolean(patch.property_id);
  if (patch.scope === "area") return Boolean(patch.area_id);
  return true;
}

export default function InstructionDetailPage() {
  const { iid } = useParams<{ iid: string }>();
  const queryClient = useQueryClient();
  const editDialogRef = useRef<HTMLDialogElement>(null);
  const [editing, setEditing] = useState(false);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [draft, setDraft] = useState<InstructionPatch>(EMPTY_PATCH);

  const instrQ = useQuery({
    queryKey: qk.instruction(iid ?? ""),
    queryFn: () => fetchJson<InstructionEnvelope>("/api/v1/instructions/" + iid).then(toInstruction),
    enabled: Boolean(iid),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
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
        body: {
          title: patch.title,
          body_md: patch.body_md,
          scope: patch.scope,
          property_id: patch.scope === "global" ? null : patch.property_id,
          area_id: patch.scope === "area" ? patch.area_id : null,
          tags: patch.tags,
          change_note: patch.change_note || null,
        },
      }).then(toInstruction),
    onSuccess: (next) => {
      queryClient.setQueryData(qk.instruction(next.id), next);
      void queryClient.invalidateQueries({ queryKey: qk.instructions() });
      void queryClient.invalidateQueries({ queryKey: qk.instructionVersions(next.id) });
      setEditing(false);
    },
  });

  useEffect(() => {
    const dialog = editDialogRef.current;
    if (!editing || !dialog) return;
    if (typeof dialog.showModal === "function") {
      try {
        if (!dialog.open) dialog.showModal();
      } catch {
        if (!dialog.open) dialog.setAttribute("open", "");
      }
      return;
    }
    if (!dialog.open) dialog.setAttribute("open", "");
  }, [editing]);

  if (!iid) return <DeskPage title="Instruction">Missing instruction id.</DeskPage>;
  if (instrQ.isPending || propsQ.isPending) {
    return <DeskPage title="Instruction"><Loading /></DeskPage>;
  }
  if (!instrQ.data || !propsQ.data) {
    return <DeskPage title="Instruction">Failed to load.</DeskPage>;
  }

  const i = instrQ.data;
  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));
  const propName = i.property_id ? propsById.get(i.property_id)?.name ?? "" : "";
  const areaName =
    i.scope === "area"
      ? areasQ.data?.find((area) => area.id === i.area)?.name ?? i.area ?? ""
      : "";
  const scopeLabel =
    i.scope === "global" ? "House-wide" :
    i.scope === "property" ? propName :
    propName + (areaName ? " · " + areaName : "");

  const sub = (
    <>
      <Link to="/instructions" className="link">← All instructions</Link>{" "}·{" "}
      <Chip tone={INSTRUCTION_SCOPE_TONE[i.scope]} size="sm">{scopeLabel}</Chip>
    </>
  );
  const actions = (
    <button
      className="btn btn--moss"
      onClick={() => {
        setDraft(toPatch(i));
        setEditing(true);
      }}
    >
      Edit
    </button>
  );
  const overflow = [
    { label: "View revisions", onSelect: () => setVersionsOpen(true) },
  ];

  function submitEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmitPatch(draft)) return;
    save.mutate(draft);
  }

  function closeEdit() {
    const dialog = editDialogRef.current;
    if (dialog?.open && typeof dialog.close === "function") {
      dialog.close();
      return;
    }
    setEditing(false);
  }

  return (
    <DeskPage title={i.title} sub={sub} actions={actions} overflow={overflow}>
      <article className="panel panel--article">
        <div className="kb-body">
          {renderBody(i.body_md)}
        </div>
        <footer className="kb-footer">
          <div>
            {i.tags.map((t) => (
              <Chip key={t} tone="ghost" size="sm">#{t}</Chip>
            ))}
          </div>
          <div className="mono muted">Revision {i.version} · saved {fmtSaved(i.updated_at)}</div>
        </footer>
      </article>

      <section className="panel">
        <header className="panel__head"><h2>Where this applies</h2></header>
        <ul className="task-list task-list--desk">
          <li className="task-row">
            <span className="task-row__time mono">via scope</span>
            <span className="task-row__title"><strong>All tasks matching the scope above</strong></span>
            <Chip tone="ghost" size="sm">automatic</Chip>
          </li>
          <li className="task-row">
            <span className="task-row__time mono">linked to template</span>
            <span className="task-row__title"><strong>Linen change — master bedroom</strong></span>
            <Chip tone="moss" size="sm">template link</Chip>
          </li>
        </ul>
      </section>

      {editing && (
        <dialog
          ref={editDialogRef}
          className="modal modal--sheet"
          onClose={() => setEditing(false)}
        >
          <form className="modal__body form" onSubmit={submitEdit}>
            <h3 className="modal__title">Edit instruction</h3>
            <label className="field">
              <span>Title</span>
              <input
                value={draft.title}
                onChange={(event) => setDraft({ ...draft, title: event.currentTarget.value })}
                required
              />
            </label>
            <div className="form-grid form-grid--two">
              <label className="field">
                <span>Scope</span>
                <select
                  value={draft.scope}
                  onChange={(event) => {
                    const scope = event.currentTarget.value as Instruction["scope"];
                    setDraft({
                      ...draft,
                      scope,
                      property_id: scope === "global" ? null : draft.property_id,
                      area_id: scope === "area" ? draft.area_id : null,
                    });
                  }}
                >
                  <option value="global">House-wide</option>
                  <option value="property">Property</option>
                  <option value="area">Area</option>
                </select>
              </label>
              <label className="field">
                <span>Property</span>
                <select
                  value={draft.property_id ?? ""}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      property_id: event.currentTarget.value || null,
                      area_id: null,
                    })
                  }
                  disabled={draft.scope === "global"}
                  required={draft.scope !== "global"}
                >
                  <option value="">House-wide</option>
                  {propsQ.data.map((p) => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
              </label>
            </div>
            {draft.scope === "area" && (
              <label className="field">
                <span>Area</span>
                <select
                  value={draft.area_id ?? ""}
                  onChange={(event) =>
                    setDraft({ ...draft, area_id: event.currentTarget.value || null })
                  }
                  disabled={!draft.property_id || areasQ.isPending}
                  required
                >
                  <option value="">
                    {draft.property_id ? "Select area" : "Select property first"}
                  </option>
                  {areasQ.data?.map((area) => (
                    <option key={area.id} value={area.id}>{area.name}</option>
                  ))}
                </select>
              </label>
            )}
            <label className="field">
              <span>Markdown</span>
              <textarea
                value={draft.body_md}
                onChange={(event) => setDraft({ ...draft, body_md: event.currentTarget.value })}
                rows={10}
              />
            </label>
            <label className="field">
              <span>Tags</span>
              <input
                value={draft.tags.join(", ")}
                onChange={(event) =>
                  setDraft({ ...draft, tags: parseTags(event.currentTarget.value) })
                }
              />
            </label>
            <label className="field">
              <span>Change note</span>
              <input
                value={draft.change_note}
                onChange={(event) =>
                  setDraft({ ...draft, change_note: event.currentTarget.value })
                }
              />
            </label>
            {save.isError && <p className="form-error">Failed to save.</p>}
            <div className="modal__actions">
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
          </form>
        </dialog>
      )}

      {versionsOpen && (
        <div className="day-drawer__scrim" onClick={() => setVersionsOpen(false)}>
          <aside
            className="day-drawer"
            role="dialog"
            aria-label="Instruction history"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="day-drawer__head">
              <div>
                <div className="day-drawer__eyebrow">Instruction history</div>
                <h2 className="day-drawer__title">{i.title}</h2>
              </div>
              <button
                type="button"
                className="day-drawer__close"
                onClick={() => setVersionsOpen(false)}
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
                    Revision {version.version} · {fmtSaved(version.created_at)}
                  </h3>
                  {version.change_note && <p className="day-drawer__muted">{version.change_note}</p>}
                  <div className="kb-body">{renderBody(version.body_md)}</div>
                </section>
              ))}
            </div>
          </aside>
        </div>
      )}
    </DeskPage>
  );
}
