import { Check, Pencil, X } from "lucide-react";
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import {
  ASSET_ICON_NAMES,
  isAssetIconName,
  type AssetIconName,
} from "@/components/AssetIcon.registry";
import { AssetIcon } from "@/components/AssetIcon";
import type { FieldRequirement } from "@/components/FormField";
import SearchField from "@/components/SearchField";

const GROUP_ROLE = "group";

interface IconSelectorProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  requirement?: FieldRequirement;
  allowEmpty?: boolean;
  disabled?: boolean;
  className?: string;
  showSelectedLabel?: boolean;
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
  showSelectedLabel = true,
  error,
  errorId,
}: IconSelectorProps) {
  const [query, setQuery] = useState("");
  const [isOpen, setIsOpen] = useState(false);
  const [popoverPlacement, setPopoverPlacement] = useState<"above" | "below">("above");
  const controlId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const previewRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const selectedName = isAssetIconName(value) ? value : "";
  const hasUnknownValue = value.trim() !== "" && !selectedName;
  const selectedLabel = hasUnknownValue ? "Unknown icon" : selectedName ? labelForIconName(selectedName) : "No icon";
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
    if (!isOpen) return;
    searchRef.current?.focus();
    searchRef.current?.select();
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;

    function closeFromOutside(event: PointerEvent): void {
      const target = event.target;
      if (target instanceof Node && rootRef.current?.contains(target)) return;
      setIsOpen(false);
    }

    document.addEventListener("pointerdown", closeFromOutside);
    return () => document.removeEventListener("pointerdown", closeFromOutside);
  }, [isOpen]);

  useEffect(() => {
    const popover = popoverRef.current;
    if (!isOpen || !popover) return undefined;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      setIsOpen(false);
      previewRef.current?.focus();
    };
    popover.addEventListener("keydown", handleKeyDown);
    return () => popover.removeEventListener("keydown", handleKeyDown);
  }, [isOpen]);

  function chooseIcon(nextName: string): void {
    if (!nextName || isAssetIconName(nextName)) {
      onChange(nextName);
      setQuery("");
      setIsOpen(false);
      previewRef.current?.focus();
    }
  }

  function openPopover(): void {
    const previewRect = previewRef.current?.getBoundingClientRect();
    setQuery("");
    setPopoverPlacement(previewRect && previewRect.top < 300 ? "below" : "above");
    setIsOpen(true);
  }

  function togglePopover(): void {
    if (isOpen) {
      setIsOpen(false);
      return;
    }
    openPopover();
  }

  function handlePreviewKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>): void {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    openPopover();
  }

  return (
    <div className={classes} ref={rootRef}>
      <span className="form-field__label">
        {label}{" "}
        <span className={`form-field__requirement form-field__requirement--${requirement}`}>
          {requirementLabel}
        </span>
      </span>

      <div className="icon-selector__control">
        <button
          type="button"
          ref={previewRef}
          className="icon-selector__selected"
          aria-haspopup="dialog"
          aria-expanded={isOpen}
          aria-controls={isOpen ? controlId : undefined}
          aria-describedby={error ? errorId : hasUnknownValue ? `${controlId}-unknown` : undefined}
          aria-label={`${label}: ${selectedLabel}. Edit icon`}
          title={`${label}: ${selectedLabel}. Edit icon`}
          disabled={disabled}
          onKeyDown={handlePreviewKeyDown}
          onClick={togglePopover}
        >
          <span className="icon-selector__selected-mark">
            {selectedName ? <AssetIcon name={selectedName} size={18} /> : <X size={16} aria-hidden="true" />}
          </span>
          {showSelectedLabel ? <strong className="icon-selector__selected-name">{selectedLabel}</strong> : null}
          <Pencil className="icon-selector__selected-edit" size={16} aria-hidden="true" />
        </button>
        {hasUnknownValue ? (
          <span id={`${controlId}-unknown`} className="sr-only">
            Saved icon is unavailable. Choose a replacement or clear it.
          </span>
        ) : null}

        {isOpen ? (
          <div
            ref={popoverRef}
            id={controlId}
            className={`icon-selector__popover icon-selector__popover--${popoverPlacement}`}
            role={GROUP_ROLE}
            aria-label={`${label} choices`}
          >
            <SearchField
              ref={searchRef}
              value={query}
              disabled={disabled}
              invalid={Boolean(error)}
              aria-label={`Search ${label.toLocaleLowerCase()} choices`}
              aria-describedby={error ? errorId : undefined}
              onChange={(event) => setQuery(event.currentTarget.value)}
              placeholder="Search icons"
            />

            <div className="icon-selector__grid" aria-label={`${label} choices`}>
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
                  <span className="icon-selector__choice-label">No icon</span>
                  {!selectedName ? <Check className="icon-selector__choice-check" size={14} aria-hidden="true" /> : null}
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
                    <span className="icon-selector__choice-label">{option.label}</span>
                    {selected ? <Check className="icon-selector__choice-check" size={14} aria-hidden="true" /> : null}
                  </button>
                );
              })}

              {visibleOptions.length === 0 ? (
                <output className="icon-selector__empty">
                  No matching icons.
                </output>
              ) : null}
            </div>
          </div>
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
