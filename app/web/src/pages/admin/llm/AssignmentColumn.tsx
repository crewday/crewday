import { Chip } from "@/components/common";
import type { LlmCapabilityEntry } from "@/types";
import CapabilityChain from "./CapabilityChain";
import LlmUsageTotals, { formatUsageSummary } from "./LlmUsageTotals";
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
  active: Selection | null;
  setHover: SelectionSetter;
  setSelection: SelectionSetter;
  nodeClass: NodeClass;
  hasActive: boolean;
  highlighted: Highlighted;
  setRungRef: ElementRefSetter;
  onOpenCapability: (capability: string) => void;
  onOpenAssignment: (assignmentId: string) => void;
}

export default function AssignmentColumn(props: AssignmentColumnProps) {
  const {
    capabilities,
    indexes,
    active,
    setHover,
    setSelection,
    nodeClass,
    hasActive,
    highlighted,
    setRungRef,
    onOpenCapability,
    onOpenAssignment,
  } = props;
  const roots = capabilities.filter((cap) => {
    const hasChain = (indexes.assignmentsByCapability.get(cap.key) ?? []).length > 0;
    return hasChain || !indexes.inheritanceByChild.has(cap.key);
  });

  return (
    <div className="llm-graph__col llm-graph__col--assignments">
      {roots.map((cap) => {
        // code-health: ignore[ccn nloc] Capability card mapping keeps graph state, inheritance badges, and modal click targets adjacent for this column.
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
          >
            <button
              type="button"
              className="llm-graph-node__button"
              aria-label={`${cap.key} capability, ${formatUsageSummary(
                cap.calls_30d,
                cap.spend_usd_30d,
              )}`}
              onFocus={() => setHover({ column: "capability", id: cap.key })}
              onBlur={() => setHover(null)}
              onClick={() =>
                onOpenCapability(cap.key)
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
                ) : null}
              </header>
              <div className="llm-graph-node__meta">{cap.description}</div>
              {isInheriting ? (
                <div className="llm-graph-node__inherits">
                  {hasExplicitInheritance ? "explicitly inherits" : "defaults"} to{" "}
                  <code className="inline-code">{inheritsFrom}</code>
                </div>
              ) : null}
              <LlmUsageTotals spendUsd={cap.spend_usd_30d} calls={cap.calls_30d} />
            </button>
            <CapabilityChain
              chain={chain}
              indexes={indexes}
              active={active}
              hasActive={hasActive}
              highlighted={highlighted}
              setHover={setHover}
              setSelection={setSelection}
              setRungRef={setRungRef}
              onOpenAssignment={onOpenAssignment}
            />
            {inheritedChildren.length ? (
              <div className="llm-graph-node__children">
                {inheritedChildren.map((child) => {
                  // code-health: ignore[ccn nloc] Inherited-child row mapping is a compact visual state mapper over one capability edge.
                  const missing = indexes.issuesByCapability.get(child.key) ?? [];
                  const childActive =
                    active?.column === "capability" && active.id === child.key;
                  const childLinked = highlighted.capabilities.has(child.key);
                  return (
                    <button
                      key={child.key}
                      type="button"
                      className={[
                        "llm-graph-node__child",
                        childActive ? "is-active" : "",
                        childLinked && !childActive ? "is-linked" : "",
                        hasActive && !childLinked ? "is-dim" : "",
                        missing.length ? "is-error" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      onMouseEnter={(e) => {
                        e.stopPropagation();
                        setHover({ column: "capability", id: child.key });
                      }}
                      onMouseLeave={() => setHover(null)}
                      onFocus={(e) => {
                        e.stopPropagation();
                        setHover({ column: "capability", id: child.key });
                      }}
                      onBlur={() => setHover(null)}
                      onClick={(e) => {
                        e.stopPropagation();
                        setSelection({ column: "capability", id: child.key });
                        onOpenCapability(child.key);
                      }}
                      title={
                        missing.length
                          ? `Inherited model lacks ${missing.join(", ")}`
                          : `Inherits from ${cap.key}`
                      }
                      aria-label={`Open assignment and inheritance settings for ${child.key}, inherited from ${cap.key}${
                        missing.length
                          ? `, inherited model lacks ${missing.join(", ")}`
                          : ""
                      }, ${formatUsageSummary(child.calls_30d, child.spend_usd_30d)}`}
                    >
                      <code className="inline-code">{child.key}</code>
                      <LlmUsageTotals
                        spendUsd={child.spend_usd_30d}
                        calls={child.calls_30d}
                      />
                    </button>
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
