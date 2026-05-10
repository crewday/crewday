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
  onChangeInheritance: (
    capability: string,
    inheritsFrom: string,
    isExplicit: boolean,
  ) => void;
  onRemoveInheritance: (capability: string) => void;
}

export default function AssignmentColumn(props: AssignmentColumnProps) {
  const {
    capabilities,
    indexes,
    selection,
    setHover,
    setSelection,
    nodeClass,
    hasActive,
    highlighted,
    setRungRef,
    onChangeInheritance,
    onRemoveInheritance,
  } = props;

  const roots = capabilities.filter((cap) => {
    const hasChain = (indexes.assignmentsByCapability.get(cap.key) ?? []).length > 0;
    return hasChain || !indexes.inheritanceByChild.has(cap.key);
  });

  return (
    <div className="llm-graph__col">
      {roots.map((cap) => {
        const chain = indexes.assignmentsByCapability.get(cap.key) ?? [];
        const inheritsFrom = indexes.inheritanceByChild.get(cap.key);
        const hasExplicitInheritance = indexes.explicitInheritanceByChild.has(cap.key);
        const inheritedMissing = indexes.issuesByCapability.get(cap.key) ?? [];
        const inheritedChildren = indexes.childrenByParent
          .get(cap.key)
          ?.map((key) => indexes.capabilitiesByKey.get(key))
          .filter((child): child is LlmCapabilityEntry => Boolean(child)) ?? [];
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
              ) : inheritedMissing.length && isInheriting ? (
                <Chip tone="rust" size="sm">
                  inherited model lacks {inheritedMissing.join(", ")}
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
                {hasExplicitInheritance ? "explicitly inherits" : "defaults"} to{" "}
                <code className="inline-code">{inheritsFrom}</code>
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
            {inheritedChildren.length ? (
              <div className="llm-graph-node__children">
                {inheritedChildren.map((child) => {
                  const missing = indexes.issuesByCapability.get(child.key) ?? [];
                  const parent = indexes.inheritanceByChild.get(child.key) ?? cap.key;
                  const isExplicit = indexes.explicitInheritanceByChild.has(child.key);
                  const parentOptions = capabilities.filter(
                    (candidate) => candidate.key !== child.key,
                  );
                  return (
                    <div
                      key={child.key}
                      className={[
                        "llm-graph-node__child",
                        missing.length ? "is-error" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      onMouseEnter={(e) => {
                        e.stopPropagation();
                        setHover({ column: "capability", id: child.key });
                      }}
                      onClick={(e) => {
                        e.stopPropagation();
                        setSelection({ column: "capability", id: child.key });
                      }}
                      title={
                        missing.length
                          ? `Inherited model lacks ${missing.join(", ")}`
                          : `Inherits from ${cap.key}`
                      }
                    >
                      <button
                        className="llm-graph-node__child-main"
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          setSelection({ column: "capability", id: child.key });
                        }}
                      >
                        <code className="inline-code">{child.key}</code>
                      </button>
                      <Chip tone={missing.length ? "rust" : "sand"} size="sm">
                        {isExplicit
                          ? missing.length
                            ? "invalid explicit"
                            : "explicit"
                          : missing.length
                            ? "invalid implicit"
                            : "implicit default"}
                      </Chip>
                      <select
                        className="llm-graph-node__inherit-select"
                        aria-label={`Change ${child.key} inheritance parent`}
                        value={parent}
                        onClick={(e) => e.stopPropagation()}
                        onChange={(e) => {
                          onChangeInheritance(
                            child.key,
                            e.currentTarget.value,
                            isExplicit,
                          );
                        }}
                      >
                        {parentOptions.map((option) => (
                          <option key={option.key} value={option.key}>
                            {option.key}
                          </option>
                        ))}
                      </select>
                      {isExplicit ? (
                        <button
                          className="llm-graph-node__inherit-remove"
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            onRemoveInheritance(child.key);
                          }}
                        >
                          Remove
                        </button>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}
