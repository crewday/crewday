import { type AriaAttributes, type ReactNode, useEffect, useId, useMemo, useRef, useState } from "react";
import FormField, { type FieldRequirement } from "@/components/FormField";

export interface SearchableSelectOption {
  value: string;
  label: string;
  secondaryText?: string;
  searchText?: string;
}

interface SearchableSelectBlankOption {
  label: string;
  secondaryText?: string;
  searchText?: string;
}

interface SearchableSelectProps {
  label: string;
  value: string;
  options: readonly SearchableSelectOption[];
  onChange: (value: string) => void;
  id?: string;
  name?: string;
  disabled?: boolean;
  required?: boolean;
  requirement?: FieldRequirement;
  className?: string;
  inputClassName?: string;
  helpId?: string;
  helpText?: ReactNode;
  blankOption?: SearchableSelectBlankOption;
  placeholder?: string;
  noResultsLabel?: string;
  renderOptionSecondaryText?: (option: SearchableSelectOption) => ReactNode;
  "aria-describedby"?: string;
  "aria-invalid"?: AriaAttributes["aria-invalid"];
}

const MAX_VISIBLE_OPTIONS = 40;

export default function SearchableSelect({
  label,
  value,
  options,
  onChange,
  id,
  name,
  disabled = false,
  required = false,
  requirement,
  className,
  inputClassName,
  helpId,
  helpText,
  blankOption,
  placeholder = "Search...",
  noResultsLabel = "No matches",
  renderOptionSecondaryText = defaultOptionSecondaryText,
  "aria-describedby": ariaDescribedBy,
  "aria-invalid": ariaInvalid,
}: SearchableSelectProps) {
  const generatedFieldId = useId();
  const fieldId = id ?? generatedFieldId;
  const listboxId = useId();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const selectOptions = useMemo(() => withBlankOption(options, blankOption), [blankOption, options]);
  const [query, setQuery] = useState(() => selectedLabel(selectOptions, value));
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);

  const selectedOption = selectOptions.find((option) => option.value === value);
  const selectedOptionLabel = selectedOption?.label ?? "";
  const filterQuery = open && query === selectedOptionLabel ? "" : query;
  const filteredOptions = useMemo(() => filterOptions(selectOptions, filterQuery), [selectOptions, filterQuery]);
  const selectedOptionIndex = selectOptions.findIndex((option) => option.value === selectedOption?.value);
  const visibleStartIndex = selectedOptionIndex >= MAX_VISIBLE_OPTIONS && filterQuery === ""
    ? Math.min(selectedOptionIndex, Math.max(filteredOptions.length - MAX_VISIBLE_OPTIONS, 0))
    : 0;
  const visibleOptions = filteredOptions.slice(visibleStartIndex, visibleStartIndex + MAX_VISIBLE_OPTIONS);
  const selectedOpenStartIndex = selectedOptionIndex >= MAX_VISIBLE_OPTIONS
    ? Math.min(selectedOptionIndex, Math.max(selectOptions.length - MAX_VISIBLE_OPTIONS, 0))
    : 0;
  const selectedOpenActiveIndex = selectedOptionIndex >= 0 ? selectedOptionIndex - selectedOpenStartIndex : 0;
  const hasUncommittedQuery = query.trim() !== "" && query !== selectedOptionLabel;
  const invalidSelection = required && (
    !selectedOption ||
    selectedOption.value === "" ||
    query.trim() === "" ||
    hasUncommittedQuery
  );
  const isInvalid = invalidSelection || ariaInvalid === true || ariaInvalid === "true";
  const describedBy = joinedIds(ariaDescribedBy, helpText ? helpId : undefined);
  const fieldClassName = [
    "searchable-select",
    disabled ? "searchable-select--disabled" : null,
    className,
  ].filter(Boolean).join(" ");
  const inputClasses = ["searchable-select__input", inputClassName].filter(Boolean).join(" ");

  useEffect(() => {
    if (document.activeElement !== inputRef.current) {
      setQuery(selectedLabel(selectOptions, value));
    }
  }, [selectOptions, value]);

  useEffect(() => {
    inputRef.current?.setCustomValidity(invalidSelection ? `Select a ${label.toLocaleLowerCase()}.` : "");
  }, [invalidSelection, label]);

  function commit(option: SearchableSelectOption) {
    if (disabled) return;
    onChange(option.value);
    setQuery(option.label);
    setOpen(false);
  }

  function resetToSelected() {
    setQuery(selectedLabel(selectOptions, value));
    setOpen(false);
  }

  function openAtSelected() {
    setOpen(true);
    setActiveIndex(selectedOpenActiveIndex);
  }

  return (
    <FormField
      label={label}
      requirement={requirement ?? (required ? "required" : "optional")}
      className={fieldClassName}
      helpId={helpId}
      helpText={helpText}
    >
      <input
        ref={inputRef}
        id={fieldId}
        className={inputClasses}
        role="combobox"
        type="text"
        value={query}
        disabled={disabled}
        required={required}
        placeholder={placeholder}
        autoComplete="off"
        aria-autocomplete="list"
        aria-required={required || undefined}
        aria-expanded={open}
        aria-invalid={isInvalid ? true : ariaInvalid}
        aria-describedby={describedBy}
        aria-controls={listboxId}
        aria-activedescendant={open && visibleOptions[activeIndex] ? optionId(listboxId, activeIndex) : undefined}
        onFocus={() => {
          if (!disabled) openAtSelected();
        }}
        onBlur={resetToSelected}
        onChange={(event) => {
          setQuery(event.currentTarget.value);
          setOpen(true);
          setActiveIndex(0);
        }}
        onKeyDown={(event) => {
          if (disabled) return;
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setOpen(true);
            setActiveIndex((index) => {
              const maxIndex = Math.max(visibleOptions.length - 1, 0);
              const startIndex = open ? index : selectedOpenActiveIndex;
              return Math.min(Math.max(startIndex, 0) + 1, maxIndex);
            });
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
            setActiveIndex((index) => {
              const maxIndex = Math.max(visibleOptions.length - 1, 0);
              const startIndex = open ? index : selectedOptionIndex >= 0 ? selectedOpenActiveIndex : maxIndex;
              return Math.max(Math.min(startIndex, maxIndex) - 1, 0);
            });
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
      {name ? <input type="hidden" name={name} value={value} disabled={disabled} /> : null}
      {open && (
        <div className="searchable-select__popover">
          <ul id={listboxId} className="searchable-select__list" role="listbox" aria-label={label}>
            {visibleOptions.map((option, index) => {
              const secondaryText = renderOptionSecondaryText(option);
              return (
                <li
                  id={optionId(listboxId, index)}
                  key={option.value}
                  role="option"
                  aria-selected={option.value === selectedOption?.value}
                  className={index === activeIndex ? "searchable-select__option is-active" : "searchable-select__option"}
                  onMouseEnter={() => setActiveIndex(index)}
                  onMouseDown={(event) => {
                    event.preventDefault();
                    commit(option);
                  }}
                >
                  <span className="searchable-select__label">{option.label}</span>
                  {secondaryText ? <span className="searchable-select__value">{secondaryText}</span> : null}
                </li>
              );
            })}
            {visibleOptions.length === 0 && (
              <li className="searchable-select__empty" aria-live="polite">{noResultsLabel}</li>
            )}
          </ul>
        </div>
      )}
    </FormField>
  );
}

function withBlankOption(
  options: readonly SearchableSelectOption[],
  blankOption: SearchableSelectBlankOption | undefined,
): SearchableSelectOption[] {
  if (!blankOption) return [...options];
  const option = { value: "", ...blankOption };
  return [option, ...options.filter((item) => item.value !== "")];
}

function defaultOptionSecondaryText(option: SearchableSelectOption): ReactNode {
  return option.secondaryText ?? option.value;
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

function joinedIds(...ids: Array<string | false | undefined>): string | undefined {
  const joined = ids.filter(Boolean).join(" ");
  return joined || undefined;
}
