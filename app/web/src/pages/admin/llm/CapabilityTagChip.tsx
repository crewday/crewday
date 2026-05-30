import { capabilityTagLabel } from "./CapabilityTagChip.lib";

export default function CapabilityTagChip({ tag }: { tag: string }) {
  return (
    <span
      className="chip chip--sm llm-capability-tag"
      data-capability-tag={tag}
    >
      {capabilityTagLabel(tag)}
    </span>
  );
}
