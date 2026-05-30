import { Search } from "lucide-react";
import { type ComponentPropsWithoutRef, type ReactNode, type Ref } from "react";

export interface SearchFieldProps
  extends Omit<ComponentPropsWithoutRef<"input">, "children" | "className" | "type"> {
  className?: string;
  invalid?: boolean;
  label?: ReactNode;
}

interface SearchFieldWithRefProps extends SearchFieldProps {
  ref?: Ref<HTMLInputElement>;
}

function SearchField({
  className,
  disabled,
  invalid = false,
  label,
  "aria-invalid": ariaInvalid,
  ref,
  ...inputProps
}: SearchFieldWithRefProps) {
  const isInvalid = invalid || ariaInvalid === true || ariaInvalid === "true";
  const classes = [
    "search-field",
    isInvalid ? "search-field--invalid" : null,
    disabled ? "search-field--disabled" : null,
    className,
  ].filter(Boolean).join(" ");

  return (
    <label className={classes}>
      <Search className="search-field__icon" size={15} aria-hidden="true" focusable="false" />
      {label ? <span className="search-field__label">{label}</span> : null}
      <input
        {...inputProps}
        ref={ref}
        type="search"
        className="search-field__input"
        disabled={disabled}
        aria-invalid={isInvalid ? true : ariaInvalid}
      />
    </label>
  );
}

export default SearchField;
