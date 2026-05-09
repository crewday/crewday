import type { ReactNode } from "react";

export type FieldRequirement = "required" | "optional";

interface FormFieldProps {
  label: string;
  requirement: FieldRequirement;
  children: ReactNode;
  className?: string;
}

export default function FormField({
  label,
  requirement,
  children,
  className,
}: FormFieldProps) {
  const classes = ["field", "form-field", `form-field--${requirement}`, className]
    .filter(Boolean)
    .join(" ");
  const requirementClasses = [
    "form-field__requirement",
    `form-field__requirement--${requirement}`,
  ].join(" ");
  const requirementLabel = requirement === "required" ? "Required" : "Optional";

  return (
    <label className={classes}>
      <span className="form-field__label">
        {label} <span className={requirementClasses}>{requirementLabel}</span>
      </span>
      {children}
    </label>
  );
}
