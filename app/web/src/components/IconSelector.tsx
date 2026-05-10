import { Check, Search, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  ASSET_ICON_NAMES,
  AssetIcon,
  isAssetIconName,
  type AssetIconName,
} from "@/components/AssetIcon";
import type { FieldRequirement } from "@/components/FormField";

interface IconSelectorProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  requirement?: FieldRequirement;
  allowEmpty?: boolean;
  disabled?: boolean;
  className?: string;
  error?: string;
  errorId?: string;
}

interface IconOption {
  name: AssetIconName;
  label: string;
  searchText: string;
}

const ICON_OPTIONS: readonly IconOption[] = ASSET_ICON_NAMES.map((name) => {
  const label = labelForIconName(name);
  return { name, label, searchText: `${name} ${label}` };
});

export default function IconSelector({
  label,
  value,
  onChange,
  requirement = "optional",
  allowEmpty = requirement !== "required",
  disabled = false,
  className,
  error,
  errorId,
}: IconSelectorProps) {
  const [query, setQuery] = useState("");
  const selectedName = isAssetIconName(value) ? value : "";
  const selectedLabel = selectedName ? labelForIconName(selectedName) : "No icon";
  const hasUnknownValue = value.trim() !== "" && !selectedName;
  const normalizedQuery = normalizeSearchText(query);
  const visibleOptions = useMemo(() => {
    if (!normalizedQuery) return ICON_OPTIONS;
    return ICON_OPTIONS.filter((option) => option.searchText.toLocaleLowerCase().includes(normalizedQuery));
  }, [normalizedQuery]);
  const classes = [
    "field",
    "form-field",
    "icon-selector",
    `form-field--${requirement}`,
    className,
  ].filter(Boolean).join(" ");
  const requirementLabel = requirement === "required" ? "Required" : "Optional";

  useEffect(() => {
    setQuery("");
  }, [value]);

  function chooseIcon(nextName: string): void {
    if (!nextName || isAssetIconName(nextName)) {
      onChange(nextName);
    }
  }

  return (
    <div className={classes}>
      <span className="form-field__label">
        {label}{" "}
        <span className={`form-field__requirement form-field__requirement--${requirement}`}>
          {requirementLabel}
        </span>
      </span>

      <div className="icon-selector__selected" aria-live="polite">
        <span className="icon-selector__selected-mark">
          {selectedName ? <AssetIcon name={selectedName} size={18} /> : <X size={16} aria-hidden="true" />}
        </span>
        <span className="icon-selector__selected-copy">
          <span className="icon-selector__selected-kicker">Selected icon</span>
          <strong>{selectedLabel}</strong>
          {hasUnknownValue ? (
            <span className="icon-selector__selected-note">
              Saved icon is unavailable. Choose a replacement or clear it.
            </span>
          ) : null}
        </span>
      </div>

      <label className="icon-selector__search">
        <Search size={15} aria-hidden="true" />
        <span className="sr-only">Search {label.toLocaleLowerCase()} choices</span>
        <input
          type="search"
          value={query}
          disabled={disabled}
          aria-invalid={error ? "true" : undefined}
          aria-describedby={error ? errorId : undefined}
          onChange={(event) => setQuery(event.currentTarget.value)}
          placeholder="Search icons"
        />
      </label>

      <div className="icon-selector__grid" role="group" aria-label={`${label} choices`}>
        {allowEmpty ? (
          <button
            type="button"
            className={selectedName ? "icon-selector__choice" : "icon-selector__choice is-selected"}
            aria-pressed={!selectedName}
            disabled={disabled}
            onClick={() => chooseIcon("")}
          >
            <span className="icon-selector__choice-mark">
              <X size={16} aria-hidden="true" />
            </span>
            <span>No icon</span>
            {!selectedName ? <Check size={14} aria-hidden="true" /> : null}
          </button>
        ) : null}

        {visibleOptions.map((option) => {
          const selected = option.name === selectedName;
          return (
            <button
              type="button"
              key={option.name}
              className={selected ? "icon-selector__choice is-selected" : "icon-selector__choice"}
              aria-pressed={selected}
              aria-label={`Select ${option.label} icon`}
              disabled={disabled}
              onClick={() => chooseIcon(option.name)}
            >
              <span className="icon-selector__choice-mark">
                <AssetIcon name={option.name} size={16} />
              </span>
              <span>{option.label}</span>
              {selected ? <Check size={14} aria-hidden="true" /> : null}
            </button>
          );
        })}

        {visibleOptions.length === 0 ? (
          <p className="icon-selector__empty" role="status">
            No matching icons.
          </p>
        ) : null}
      </div>

      {error ? (
        <span id={errorId} className="form-field-error">
          {error}
        </span>
      ) : null}
    </div>
  );
}

function labelForIconName(name: string): string {
  return name.replace(/([a-z0-9])([A-Z])/g, "$1 $2");
}

function normalizeSearchText(value: string): string {
  return value.trim().toLocaleLowerCase();
}
