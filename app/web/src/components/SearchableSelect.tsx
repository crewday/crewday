import { useEffect, useId, useMemo, useRef, useState } from "react";
import FormField, { type FieldRequirement } from "@/components/FormField";

export interface SearchableSelectOption {
  value: string;
  label: string;
  searchText?: string;
}

interface SearchableSelectProps {
  label: string;
  value: string;
  options: readonly SearchableSelectOption[];
  onChange: (value: string) => void;
  required?: boolean;
  requirement?: FieldRequirement;
  placeholder?: string;
  noResultsLabel?: string;
}

const MAX_VISIBLE_OPTIONS = 40;

export default function SearchableSelect({
  label,
  value,
  options,
  onChange,
  required = false,
  requirement,
  placeholder = "Search...",
  noResultsLabel = "No matches",
}: SearchableSelectProps) {
  const fieldId = useId();
  const listboxId = useId();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [query, setQuery] = useState(() => selectedLabel(options, value));
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);

  const filteredOptions = useMemo(() => filterOptions(options, query), [options, query]);
  const visibleOptions = filteredOptions.slice(0, MAX_VISIBLE_OPTIONS);
  const selectedOption = options.find((option) => option.value === value);
  const selectedOptionLabel = selectedOption?.label ?? "";
  const hasUncommittedQuery = query.trim() !== "" && query !== selectedOptionLabel;
  const invalidSelection = required && (!selectedOption || hasUncommittedQuery);

  useEffect(() => {
    if (document.activeElement !== inputRef.current) {
      setQuery(selectedLabel(options, value));
    }
  }, [options, value]);

  useEffect(() => {
    inputRef.current?.setCustomValidity(invalidSelection ? `Select a ${label.toLocaleLowerCase()}.` : "");
  }, [invalidSelection, label]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  function commit(option: SearchableSelectOption) {
    onChange(option.value);
    setQuery(option.label);
    setOpen(false);
  }

  function resetToSelected() {
    setQuery(selectedLabel(options, value));
    setOpen(false);
  }

  return (
    <FormField
      label={label}
      requirement={requirement ?? (required ? "required" : "optional")}
      className="searchable-select"
    >
      <input
        ref={inputRef}
        id={fieldId}
        role="combobox"
        type="text"
        value={query}
        required={required}
        placeholder={placeholder}
        autoComplete="off"
        aria-autocomplete="list"
        aria-expanded={open}
        aria-invalid={invalidSelection || undefined}
        aria-controls={listboxId}
        aria-activedescendant={open && visibleOptions[activeIndex] ? optionId(listboxId, activeIndex) : undefined}
        onFocus={() => setOpen(true)}
        onBlur={resetToSelected}
        onChange={(event) => {
          setQuery(event.currentTarget.value);
          setOpen(true);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setOpen(true);
            setActiveIndex((index) => Math.min(index + 1, Math.max(visibleOptions.length - 1, 0)));
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
            setActiveIndex((index) => Math.max(index - 1, 0));
          } else if (event.key === "Enter" && open) {
            event.preventDefault();
            const option = visibleOptions[activeIndex];
            if (option) commit(option);
          } else if (event.key === "Escape") {
            event.preventDefault();
            resetToSelected();
          }
        }}
      />
      {open && (
        <div className="searchable-select__popover">
          <ul id={listboxId} className="searchable-select__list" role="listbox" aria-label={label}>
            {visibleOptions.map((option, index) => (
              <li
                id={optionId(listboxId, index)}
                key={option.value}
                role="option"
                aria-selected={option.value === selectedOption?.value}
                className={index === activeIndex ? "searchable-select__option is-active" : "searchable-select__option"}
                onMouseDown={(event) => {
                  event.preventDefault();
                  commit(option);
                }}
              >
                <span className="searchable-select__label">{option.label}</span>
                <span className="searchable-select__value">{option.value}</span>
              </li>
            ))}
            {visibleOptions.length === 0 && (
              <li className="searchable-select__empty" aria-live="polite">{noResultsLabel}</li>
            )}
          </ul>
        </div>
      )}
    </FormField>
  );
}

function selectedLabel(options: readonly SearchableSelectOption[], value: string): string {
  return options.find((option) => option.value === value)?.label ?? value;
}

function filterOptions(
  options: readonly SearchableSelectOption[],
  query: string,
): SearchableSelectOption[] {
  const normalized = normalizeSearchText(query);
  if (!normalized) return [...options];
  return options.filter((option) =>
    normalizeSearchText(`${option.label} ${option.value} ${option.searchText ?? ""}`).includes(normalized)
  );
}

function normalizeSearchText(value: string): string {
  return value.trim().toLocaleLowerCase();
}

function optionId(listboxId: string, index: number): string {
  return `${listboxId}-option-${index}`;
}
