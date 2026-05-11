import { Search } from "lucide-react";
import { forwardRef, type ComponentPropsWithoutRef, type ReactNode } from "react";

export interface SearchFieldProps
  extends Omit<ComponentPropsWithoutRef<"input">, "children" | "className" | "type"> {
  className?: string;
  invalid?: boolean;
  label?: ReactNode;
}

const SearchField = forwardRef<HTMLInputElement, SearchFieldProps>(function SearchField(
  {
    className,
    disabled,
    invalid = false,
    label,
    "aria-invalid": ariaInvalid,
    ...inputProps
  },
  ref,
) {
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
});

export default SearchField;
