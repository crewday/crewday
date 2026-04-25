import { useQuery } from "@tanstack/react-query";
import { Camera, Globe, Hash, Map as MapIcon, Sparkles, Timer } from "lucide-react";

import { Chip, Loading } from "@/components/common";
import DeskPage from "@/components/DeskPage";
import { fetchJson } from "@/lib/api";
import { type ListEnvelope } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import type { ChecklistTemplateItem, TaskPriority, TaskTemplate } from "@/types/task";
import type { WorkRole } from "@/types/employee";

// §08 — decimal qty formatter, trailing zeros trimmed. The wire shape
// is currently integer-only (`inventory_consumption_json: dict[str,
// int]`), but the spec calls for decimals (0.3 bottles of window-
// washer) and the renderer is the same on both sides — so the helper
// stays decimal-aware now to spare a rewrite when storage widens.
function fmtQty(n: number): string {
  const s = n.toFixed(3);
  return s.replace(/\.?0+$/, "");
}

const PRIORITY_TONE: Record<TaskPriority, "ghost" | "sand" | "rust"> = {
  low: "ghost",
  normal: "ghost",
  high: "sand",
  urgent: "rust",
};

const PROPERTY_SCOPE_LABEL: Record<TaskTemplate["property_scope"], string> = {
  any: "Any property",
  one: "One property",
  listed: "Listed properties",
};

const AREA_SCOPE_LABEL: Record<TaskTemplate["area_scope"], string> = {
  any: "Any area",
  one: "One area",
  listed: "Listed areas",
};

const HINTS_MAX_CHARS = 140;

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max).trimEnd()}…`;
}

// Strip the lightest layer of Markdown so the card preview reads as
// plain prose. Goes no deeper than that — a real Markdown renderer
// belongs in the template-detail drawer (out of scope for the list
// card).
function plainPreview(md: string): string {
  return md
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/_([^_]+)_/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

export default function TemplatesPage() {
  const tplQ = useQuery({
    queryKey: qk.taskTemplates(),
    queryFn: () =>
      fetchJson<ListEnvelope<TaskTemplate>>("/api/v1/task_templates"),
  });
  const rolesQ = useQuery({
    queryKey: qk.workRoles(),
    queryFn: () => fetchJson<ListEnvelope<WorkRole>>("/api/v1/work_roles"),
  });

  if (tplQ.isPending || rolesQ.isPending) {
    return (
      <DeskPage title="Task templates" actions={<button className="btn btn--moss">+ New template</button>}>
        <Loading />
      </DeskPage>
    );
  }
  if (!tplQ.data || !rolesQ.data) {
    return (
      <DeskPage title="Task templates" actions={<button className="btn btn--moss">+ New template</button>}>
        Failed to load.
      </DeskPage>
    );
  }

  const templates = tplQ.data.data;
  const rolesById = new Map(rolesQ.data.data.map((r) => [r.id, r]));

  return (
    <DeskPage
      title="Task templates"
      sub="Reusable definitions. Schedules materialize tasks from these. Edit once, update everywhere."
      actions={<button className="btn btn--moss">+ New template</button>}
    >
      <section className="grid grid--cards">
        {templates.map((tpl) => {
          const role = tpl.role_id ? rolesById.get(tpl.role_id) : undefined;
          const roleLabel = role?.name ?? (tpl.role_id ? "Unknown role" : "Any role");
          const propertyScopeLabel = renderPropertyScope(tpl);
          const areaScopeLabel = renderAreaScope(tpl);
          const desc = plainPreview(tpl.description_md);
          const checklist = tpl.checklist_template_json;
          const hints = tpl.llm_hints_md ? plainPreview(tpl.llm_hints_md) : "";
          return (
            <article key={tpl.id} className="tpl-card">
              <header className="tpl-card__head">
                <h3 className="tpl-card__title">{tpl.name}</h3>
                <div className="tpl-card__chips">
                  <Chip tone="ghost" size="sm">{roleLabel}</Chip>
                  <Chip tone={PRIORITY_TONE[tpl.priority]} size="sm">{tpl.priority}</Chip>
                  {tpl.photo_evidence !== "disabled" && (
                    <Chip tone="sky" size="sm">
                      <Camera size={12} strokeWidth={1.8} aria-hidden="true" /> {tpl.photo_evidence}
                    </Chip>
                  )}
                </div>
              </header>
              {desc && <p className="tpl-card__desc">{desc}</p>}
              <div className="tpl-card__meta">
                <span className="tpl-card__duration">
                  <Timer size={14} strokeWidth={1.75} aria-hidden="true" /> {tpl.duration_minutes} min
                </span>
                <span className="tpl-card__scope">
                  <Globe size={12} strokeWidth={1.75} aria-hidden="true" /> {propertyScopeLabel}
                </span>
                <span className="tpl-card__scope">
                  <MapIcon size={12} strokeWidth={1.75} aria-hidden="true" /> {areaScopeLabel}
                </span>
                {tpl.linked_instruction_ids.length > 0 && (
                  <span className="tpl-card__scope">
                    <Hash size={12} strokeWidth={1.75} aria-hidden="true" /> {tpl.linked_instruction_ids.length} linked
                  </span>
                )}
              </div>
              {checklist.length > 0 && (
                <ul className="tpl-card__checklist">
                  {checklist.map((c) => (
                    <li key={c.key}>
                      <span className="checklist__box" aria-hidden="true" />
                      <span>{renderChecklistLabel(c)}</span>
                      {c.required && <Chip tone="rust" size="sm">required</Chip>}
                      {c.guest_visible && <Chip tone="moss" size="sm">guest-visible</Chip>}
                      {c.rrule && <Chip tone="sand" size="sm">RRULE</Chip>}
                    </li>
                  ))}
                </ul>
              )}
              {tpl.inventory_effects.length > 0 && (
                <div className="tpl-card__effects">
                  {tpl.inventory_effects.some((e) => e.kind === "consume") && (
                    <div className="tpl-effect tpl-effect--consume">
                      <span className="tpl-effect__label">Uses</span>
                      <span>
                        {tpl.inventory_effects
                          .filter((e) => e.kind === "consume")
                          .map((e) => `${fmtQty(e.qty)} ${e.item_ref}`)
                          .join(" · ")}
                      </span>
                    </div>
                  )}
                  {tpl.inventory_effects.some((e) => e.kind === "produce") && (
                    <div className="tpl-effect tpl-effect--produce">
                      <span className="tpl-effect__label">Produces</span>
                      <span>
                        {tpl.inventory_effects
                          .filter((e) => e.kind === "produce")
                          .map((e) => `${fmtQty(e.qty)} ${e.item_ref}`)
                          .join(" · ")}
                      </span>
                    </div>
                  )}
                </div>
              )}
              {hints && (
                <p className="tpl-card__hints">
                  <Sparkles size={12} strokeWidth={1.75} aria-hidden="true" />
                  <span>{truncate(hints, HINTS_MAX_CHARS)}</span>
                </p>
              )}
            </article>
          );
        })}
      </section>
    </DeskPage>
  );
}

function renderChecklistLabel(item: ChecklistTemplateItem): string {
  return item.text || item.key;
}

function renderPropertyScope(tpl: TaskTemplate): string {
  const base = PROPERTY_SCOPE_LABEL[tpl.property_scope];
  if (tpl.property_scope === "any") return base;
  return `${base} (${tpl.listed_property_ids.length})`;
}

function renderAreaScope(tpl: TaskTemplate): string {
  const base = AREA_SCOPE_LABEL[tpl.area_scope];
  if (tpl.area_scope === "any") return base;
  return `${base} (${tpl.listed_area_ids.length})`;
}
