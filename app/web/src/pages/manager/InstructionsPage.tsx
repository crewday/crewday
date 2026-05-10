import { type FormEvent, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { type ListEnvelope, unwrapList } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import { workspaceRouteForPathname } from "@/lib/workspaceRoutes";
import DeskPage from "@/components/DeskPage";
import DateTime from "@/components/DateTime";
import { Chip, Loading } from "@/components/common";
import { INSTRUCTION_SCOPE_TONE } from "@/lib/tones";
import type { Instruction, Property } from "@/types/api";

function preview(body: string): string {
  return body.length > 180 ? body.slice(0, 180) + "…" : body;
}

interface InstructionMeta {
  id: string;
  title: string;
  scope: Instruction["scope"];
  property_id: string | null;
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

interface InstructionCreateDraft {
  title: string;
  body_md: string;
  scope: Instruction["scope"];
  property_id: string | null;
  area_id: string | null;
  tags: string[];
}

const EMPTY_CREATE_DRAFT: InstructionCreateDraft = {
  title: "",
  body_md: "",
  scope: "global",
  property_id: null,
  area_id: null,
  tags: [],
};

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

function parseTags(raw: string): string[] {
  return raw
    .split(",")
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function slugFromTitle(title: string): string {
  return title
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "") || "instruction";
}

function canSubmitCreate(draft: InstructionCreateDraft): boolean {
  if (!draft.title.trim() || !draft.body_md.trim()) return false;
  if (draft.scope === "property") return Boolean(draft.property_id);
  if (draft.scope === "area") return Boolean(draft.property_id && draft.area_id);
  return true;
}

export default function InstructionsPage() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const createDialogRef = useRef<HTMLDialogElement>(null);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<InstructionCreateDraft>(EMPTY_CREATE_DRAFT);

  const instrQ = useQuery({
    queryKey: qk.instructions(),
    queryFn: () => fetchJson<ListEnvelope<Instruction>>("/api/v1/instructions").then(unwrapList),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const areasQ = useQuery({
    queryKey: qk.propertyAreas(draft.property_id ?? ""),
    queryFn: () =>
      fetchJson<ListEnvelope<AreaOption>>(
        "/api/v1/properties/" + draft.property_id + "/areas",
      ).then(unwrapList),
    enabled: Boolean(creating && draft.scope === "area" && draft.property_id),
  });
  const create = useMutation({
    mutationFn: (next: InstructionCreateDraft) =>
      fetchJson<InstructionEnvelope>("/api/v1/instructions", {
        method: "POST",
        body: {
          slug: slugFromTitle(next.title),
          title: next.title,
          body_md: next.body_md,
          scope: next.scope,
          property_id: next.scope === "global" ? null : next.property_id,
          area_id: next.scope === "area" ? next.area_id : null,
          tags: next.tags,
          change_note: null,
        },
      }).then(toInstruction),
    onSuccess: (instruction) => {
      queryClient.setQueryData(qk.instruction(instruction.id), instruction);
      void queryClient.invalidateQueries({ queryKey: qk.instructions() });
      setCreating(false);
      navigate(workspaceRouteForPathname(pathname, "/instructions/" + instruction.id));
    },
  });

  useEffect(() => {
    const dialog = createDialogRef.current;
    if (!creating || !dialog) return;
    if (typeof dialog.showModal === "function") {
      try {
        if (!dialog.open) dialog.showModal();
      } catch {
        if (!dialog.open) dialog.setAttribute("open", "");
      }
      return;
    }
    if (!dialog.open) dialog.setAttribute("open", "");
  }, [creating]);

  const sub = "The house knowledge base. Global rules, property quirks, area-specific tips. Staff see the ones that apply to their task.";
  const actions = (
    <button
      className="btn btn--moss"
      onClick={() => {
        setDraft(EMPTY_CREATE_DRAFT);
        create.reset();
        setCreating(true);
      }}
    >
      + New instruction
    </button>
  );

  function submitCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmitCreate(draft)) return;
    create.mutate(draft);
  }

  function closeCreate() {
    const dialog = createDialogRef.current;
    if (dialog?.open && typeof dialog.close === "function") {
      dialog.close();
      return;
    }
    setCreating(false);
  }

  function renderCreateDialog() {
    if (!creating) return null;
    return (
      <dialog
        ref={createDialogRef}
        className="modal modal--sheet"
        aria-label="Create instruction"
        onClose={() => setCreating(false)}
      >
        <form className="modal__body form" onSubmit={submitCreate}>
          <h3 className="modal__title">Create instruction</h3>
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
                {(propsQ.data ?? []).map((p) => (
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
              required
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
          {create.isError && <p className="form-error">Failed to create instruction.</p>}
          <div className="modal__actions">
            <button type="button" className="btn btn--ghost" onClick={closeCreate}>
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn--moss"
              disabled={create.isPending || !canSubmitCreate(draft)}
            >
              Create
            </button>
          </div>
        </form>
      </dialog>
    );
  }

  if (instrQ.isPending || propsQ.isPending) {
    return (
      <DeskPage title="Instructions" sub={sub} actions={actions}>
        <Loading />
        {renderCreateDialog()}
      </DeskPage>
    );
  }
  if (!instrQ.data || !propsQ.data) {
    return (
      <DeskPage title="Instructions" sub={sub} actions={actions}>
        Failed to load.
        {renderCreateDialog()}
      </DeskPage>
    );
  }

  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));
  const instructions = instrQ.data;
  const countBy = (scope: Instruction["scope"]): number =>
    instructions.filter((i) => i.scope === scope).length;

  const scopeLabel = (i: Instruction): string => {
    if (i.scope === "global") return "House-wide";
    const propName = i.property_id ? propsById.get(i.property_id)?.name ?? "" : "";
    if (i.scope === "property") return propName;
    return propName + (i.area ? " · " + i.area : "");
  };

  return (
    <DeskPage title="Instructions" sub={sub} actions={actions}>
      <section className="panel">
        <div className="desk-filters">
          <span className="chip chip--ghost chip--sm chip--active">All</span>
          <span className="chip chip--ghost chip--sm">Global · {countBy("global")}</span>
          <span className="chip chip--ghost chip--sm">Property · {countBy("property")}</span>
          <span className="chip chip--ghost chip--sm">Area · {countBy("area")}</span>
        </div>

        <ul className="kb-list">
          {instructions.map((i) => (
            <li key={i.id} className="kb-item">
              <Link to={workspaceRouteForPathname(pathname, "/instructions/" + i.id)} className="kb-item__main">
                <div className="kb-item__head">
                  <h3 className="kb-item__title">{i.title}</h3>
                  <Chip tone={INSTRUCTION_SCOPE_TONE[i.scope]} size="sm">{scopeLabel(i)}</Chip>
                </div>
                <p className="kb-item__preview">{preview(i.body_md)}</p>
                <div className="kb-item__meta">
                  {i.tags.map((t) => (
                    <Chip key={t} tone="ghost" size="sm">#{t}</Chip>
                  ))}
                  <span className="mono muted">
                    v{i.version} · updated <DateTime value={i.updated_at} />
                  </span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      </section>
      {renderCreateDialog()}
    </DeskPage>
  );
}
