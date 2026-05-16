import type { ReactNode } from "react";

interface RegistryCheckPillProps {
  checked: boolean;
  children: ReactNode;
  onChange: (checked: boolean) => void;
}

export default function RegistryCheckPill({
  checked,
  children,
  onChange,
}: RegistryCheckPillProps) {
  const className = [
    "llm-registry-form__check",
    checked
      ? "llm-registry-form__check--checked"
      : "llm-registry-form__check--unchecked",
  ].join(" ");

  return (
    <label className={className}>
      <input
        type="checkbox"
        className="llm-registry-form__check-input"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>{children}</span>
    </label>
  );
}
