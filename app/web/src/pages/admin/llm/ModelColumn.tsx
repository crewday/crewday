import { Chip } from "@/components/common";
import type { LlmModel } from "@/types";
import type { ElementRefSetter, NodeClass, Selection, SelectionSetter } from "./types";

const CAPABILITY_TAG_LABEL: Record<string, string> = {
  chat: "chat",
  vision: "vision",
  audio_input: "audio",
  reasoning: "reasoning",
  function_calling: "tools",
  json_mode: "json",
  streaming: "stream",
};

interface ModelColumnProps {
  models: LlmModel[];
  selection: Selection | null;
  setHover: SelectionSetter;
  setSelection: SelectionSetter;
  nodeClass: NodeClass;
  setModelRef: ElementRefSetter;
  onModelClick: (modelId: string) => boolean;
}

export default function ModelColumn({
  models,
  selection,
  setHover,
  setSelection,
  nodeClass,
  setModelRef,
  onModelClick,
}: ModelColumnProps) {
  return (
    <div className="llm-graph__col">
      {models.map((m) => (
        <article
          key={m.id}
          ref={setModelRef(m.id)}
          className={nodeClass("model", m.id)}
          onMouseEnter={() => setHover({ column: "model", id: m.id })}
          onMouseLeave={() => setHover(null)}
          onClick={() => {
            if (onModelClick(m.id)) return;
            setSelection(
              selection?.column === "model" && selection.id === m.id
                ? null
                : { column: "model", id: m.id },
            );
          }}
        >
          <header className="llm-graph-node__head">
            <span className="llm-graph-node__name">{m.display_name}</span>
            <span className="llm-graph-node__vendor">{m.vendor}</span>
          </header>
          <div className="llm-graph-node__meta mono">{m.canonical_name}</div>
          <div className="llm-graph-node__tags">
            {m.capabilities.map((tag) => (
              <Chip key={tag} tone="ghost" size="sm">
                {CAPABILITY_TAG_LABEL[tag] ?? tag}
              </Chip>
            ))}
          </div>
          <footer className="llm-graph-node__foot">
            <span>
              {m.provider_model_count} provider
              {m.provider_model_count === 1 ? "" : "s"}
            </span>
            {m.context_window ? (
              <span className="muted">{(m.context_window / 1000).toFixed(0)}k ctx</span>
            ) : null}
          </footer>
        </article>
      ))}
    </div>
  );
}
