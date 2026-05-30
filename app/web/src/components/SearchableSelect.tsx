import {
  type AriaAttributes,
  type KeyboardEvent,
  type ReactNode,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import FormField, { type FieldRequirement } from "@/components/FormField";

const COMBOBOX_ROLE = "combobox";
const LISTBOX_ROLE = "listbox";
const OPTION_ROLE = "option";

export interface SearchableSelectOption {
  value: string;
  label: string;
  secondaryText?: string;
  searchText?: string;
  disabled?: boolean;
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
  onInputKeyDown?: (event: KeyboardEvent<HTMLInputElement>) => void;
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
  onInputKeyDown,
  "aria-describedby": ariaDescribedBy,
  "aria-invalid": ariaInvalid,
}: SearchableSelectProps) {
  const generatedFieldId = useId();
  const fieldId = id ?? generatedFieldId;
  const listboxId = useId();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const activeIndexRef = useRef(0);
  const selectOptions = useMemo(() => withBlankOption(options, blankOption), [blankOption, options]);
  const [draftQuery, setDraftQuery] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);

  const selectedOption = selectOptions.find((option) => option.value === value);
  const selectedOptionLabel = selectedOption?.label ?? "";
  const query = draftQuery ?? selectedOptionLabel;
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
  const invalidSelection = !disabled && required && (
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
    inputRef.current?.setCustomValidity(invalidSelection ? `Select a ${label.toLocaleLowerCase()}.` : "");
  }, [invalidSelection, label]);

  function commit(option: SearchableSelectOption) {
    if (disabled || option.disabled) return;
    onChange(option.value);
    setDraftQuery(option.label);
    setOpen(false);
  }

  function resetToSelected() {
    setDraftQuery(null);
    setOpen(false);
  }

  function openAtSelected() {
    setOpen(true);
    if (selectedOption?.disabled) {
      updateActiveIndex(firstEnabledIndex(
        selectOptions.slice(
          selectedOpenStartIndex,
          selectedOpenStartIndex + MAX_VISIBLE_OPTIONS,
        ),
      ));
      return;
    }
    updateActiveIndex(selectedOpenActiveIndex);
  }

  function moveActive(direction: -1 | 1) {
    setOpen(true);
    setActiveIndex((index) => {
      const maxIndex = Math.max(visibleOptions.length - 1, 0);
      const startIndex = open
        ? index
        : direction === -1 && selectedOptionIndex < 0
          ? maxIndex
          : selectedOpenActiveIndex;
      const nextIndex = nextEnabledIndex(visibleOptions, startIndex, direction, maxIndex);
      activeIndexRef.current = nextIndex;
      return nextIndex;
    });
  }

  function updateActiveIndex(nextIndex: number) {
    activeIndexRef.current = nextIndex;
    setActiveIndex(nextIndex);
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
        role={COMBOBOX_ROLE}
        type="text"
        value={query}
        disabled={disabled}
        required={required}
        placeholder={placeholder}
        autoComplete="off"
        aria-autocomplete="list"
        aria-label={label}
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
          const nextQuery = event.currentTarget.value;
          setDraftQuery(nextQuery);
          setOpen(true);
          updateActiveIndex(firstEnabledIndex(
            filterOptions(selectOptions, nextQuery).slice(0, MAX_VISIBLE_OPTIONS),
          ));
        }}
        onKeyDown={(event) => {
          onInputKeyDown?.(event);
          if (disabled) return;
          if (event.key === "ArrowDown") {
            event.preventDefault();
            moveActive(1);
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            moveActive(-1);
          } else if (event.key === "Enter" && open) {
            event.preventDefault();
            const option = visibleOptions[activeIndexRef.current];
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
          <div id={listboxId} className="searchable-select__list" role={LISTBOX_ROLE} aria-label={label}>
            {visibleOptions.map((option, index) => {
              const secondaryText = renderOptionSecondaryText(option);
              return (
                <button
                  type="button"
                  id={optionId(listboxId, index)}
                  key={option.value}
                  role={OPTION_ROLE}
                  aria-selected={option.value === selectedOption?.value}
                  aria-disabled={option.disabled || undefined}
                  disabled={option.disabled}
                  className={[
                    "searchable-select__option",
                    index === activeIndex ? "is-active" : null,
                    option.disabled ? "is-disabled" : null,
                  ].filter(Boolean).join(" ")}
                  onMouseEnter={() => {
                    if (!option.disabled) updateActiveIndex(index);
                  }}
                  onMouseDown={(event) => {
                    event.preventDefault();
                    commit(option);
                  }}
                >
                  <span className="searchable-select__label">{option.label}</span>
                  {secondaryText ? <span className="searchable-select__value">{secondaryText}</span> : null}
                </button>
              );
            })}
            {visibleOptions.length === 0 && (
              <div className="searchable-select__empty" aria-live="polite">{noResultsLabel}</div>
            )}
          </div>
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

function nextEnabledIndex(
  options: readonly SearchableSelectOption[],
  startIndex: number,
  direction: -1 | 1,
  maxIndex: number,
): number {
  if (options.length === 0) return 0;
  let index = Math.min(Math.max(startIndex, 0), maxIndex);
  for (let steps = 0; steps < options.length; steps += 1) {
    index = Math.min(Math.max(index + direction, 0), maxIndex);
    if (!options[index]?.disabled) return index;
    if (index === 0 || index === maxIndex) break;
  }
  return Math.min(Math.max(startIndex, 0), maxIndex);
}

function firstEnabledIndex(options: readonly SearchableSelectOption[]): number {
  const index = options.findIndex((option) => !option.disabled);
  return index >= 0 ? index : 0;
}

function joinedIds(...ids: Array<string | false | undefined>): string | undefined {
  const joined = ids.filter(Boolean).join(" ");
  return joined || undefined;
}
