import { Chip } from "@/components/common";
import type { LlmCapabilityEntry } from "@/types";
import CapabilityChain from "./CapabilityChain";
import type { LlmIndexes } from "./lib/llmIndexes";
import type {
  ElementRefSetter,
  Highlighted,
  NodeClass,
  Selection,
  SelectionSetter,
} from "./types";

interface AssignmentColumnProps {
  capabilities: LlmCapabilityEntry[];
  indexes: LlmIndexes;
  selection: Selection | null;
  setHover: SelectionSetter;
  setSelection: SelectionSetter;
  nodeClass: NodeClass;
  hasActive: boolean;
  highlighted: Highlighted;
  setRungRef: ElementRefSetter;
}

export default function AssignmentColumn({
  capabilities,
  indexes,
  selection,
  setHover,
  setSelection,
  nodeClass,
  hasActive,
  highlighted,
  setRungRef,
}: AssignmentColumnProps) {
  return (
    <div className="llm-graph__col">
      {capabilities.map((cap) => {
        const chain = indexes.assignmentsByCapability.get(cap.key) ?? [];
        const inheritsFrom = indexes.inheritanceByChild.get(cap.key);
        const isUnassigned = chain.length === 0 && !inheritsFrom;
        const isInheriting = chain.length === 0 && inheritsFrom;
        return (
          <article
            key={cap.key}
            className={nodeClass("capability", cap.key)}
            onMouseEnter={() => setHover({ column: "capability", id: cap.key })}
            onMouseLeave={() => setHover(null)}
            onClick={() =>
              setSelection(
                selection?.column === "capability" && selection.id === cap.key
                  ? null
                  : { column: "capability", id: cap.key },
              )
            }
          >
            <header className="llm-graph-node__head">
              <code className="llm-graph-node__name inline-code">{cap.key}</code>
              {isUnassigned ? (
                <Chip tone="rust" size="sm">
                  unassigned
                </Chip>
              ) : isInheriting ? (
                <Chip tone="sand" size="sm">
                  inherits
                </Chip>
              ) : (
                <Chip tone="moss" size="sm">
                  {chain.length} rung{chain.length === 1 ? "" : "s"}
                </Chip>
              )}
            </header>
            <div className="llm-graph-node__meta">{cap.description}</div>
            {isInheriting ? (
              <div className="llm-graph-node__inherits">
                ↳ falls through to <code className="inline-code">{inheritsFrom}</code>
              </div>
            ) : null}
            <CapabilityChain
              chain={chain}
              indexes={indexes}
              hasActive={hasActive}
              highlighted={highlighted}
              setHover={setHover}
              setSelection={setSelection}
              setRungRef={setRungRef}
            />
          </article>
        );
      })}
    </div>
  );
}
