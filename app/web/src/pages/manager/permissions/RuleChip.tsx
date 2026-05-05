import { Chip } from "@/components/common";
import type { PermissionGroup, PermissionRule } from "@/types/api";

export default function RuleChip({
  rule,
  group,
  groupLabel,
  userLabel,
}: {
  rule: PermissionRule;
  group?: PermissionGroup;
  groupLabel?: string;
  userLabel?: string;
}) {
  const subject =
    rule.subject_kind === "group"
      ? groupLabel ?? rule.subject_id
      : userLabel ?? rule.subject_id;
  const tone = rule.effect === "allow" ? "moss" : "rust";
  return (
    <>
      <Chip tone={tone} size="sm">
        {rule.effect} · {rule.subject_kind}: {subject}
      </Chip>
      {group?.group_kind === "derived" ? (
        <span className="muted"> Auto-populated from role_grants</span>
      ) : null}
    </>
  );
}
