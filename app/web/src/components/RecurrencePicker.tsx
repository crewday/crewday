import { useMemo, useState, type FormEvent } from "react";
import { CalendarClock, Code2, Repeat2 } from "lucide-react";

import FormField from "@/components/FormField";
import FormModal, { FormModalGrid } from "@/components/FormModal";
import {
  buildRecurrenceRrule,
  frequencyFromRecurrence,
  normalizeRecurrenceRrule,
  parseRecurrenceRrule,
  recurrencePreview,
  recurrenceSummary,
  RECURRENCE_WEEKDAYS,
  type RecurrenceFrequency,
  type RecurrenceMonthlyOrdinal,
  type RecurrenceWeekday,
} from "@/components/recurrence";

type RecurrencePickerPanel = "friendly" | "advanced";
type FriendlyMode = "none" | Lowercase<RecurrenceFrequency>;
type MonthlyPattern = "monthday" | "ordinal";

interface RecurrencePickerProps {
  value: string | null;
  onChange: (value: string | null) => void;
  disabled?: boolean;
  label?: string;
  triggerLabel?: string;
  emptyLabel?: string;
  includePrefix?: boolean;
  allowNone?: boolean;
}

const FRIENDLY_OPTIONS: readonly { value: FriendlyMode; label: string }[] = [
  { value: "none", label: "Every task" },
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
  { value: "yearly", label: "Yearly" },
];

const MONTHLY_ORDINAL_OPTIONS: readonly { value: RecurrenceMonthlyOrdinal; label: string }[] = [
  { value: 1, label: "First" },
  { value: 2, label: "Second" },
  { value: 3, label: "Third" },
  { value: 4, label: "Fourth" },
  { value: -1, label: "Last" },
];

type RecurrenceEditorState = {
  panel: RecurrencePickerPanel;
  mode: FriendlyMode;
  weekdays: RecurrenceWeekday[];
  interval: number;
  monthlyPattern: MonthlyPattern;
  monthDay: number;
  monthlyOrdinal: RecurrenceMonthlyOrdinal;
  monthlyWeekday: RecurrenceWeekday;
  advanced: string;
};

function recurrenceEditorState(value: string | null): RecurrenceEditorState {
  const parsed = parseRecurrenceRrule(value);
  const frequency = frequencyFromRecurrence(value).toLowerCase() as FriendlyMode;
  return {
    panel: !parsed.value || parsed.valid && parsed.parts?.unsupported.length === 0 ? "friendly" : "advanced",
    mode: parsed.value ? frequency : "none",
    weekdays: parsed.parts?.byday.length ? parsed.parts.byday : ["MO"],
    interval: parsed.parts?.interval ?? 1,
    monthlyPattern: parsed.parts?.monthlyOrdinalWeekday ? "ordinal" : "monthday",
    monthDay: parsed.parts?.bymonthday ?? 1,
    monthlyOrdinal: parsed.parts?.monthlyOrdinalWeekday?.ordinal ?? 1,
    monthlyWeekday: parsed.parts?.monthlyOrdinalWeekday?.weekday ?? "MO",
    advanced: value ?? "",
  };
}

export default function RecurrencePicker({
  value,
  onChange,
  disabled = false,
  label = "Recurrence",
  triggerLabel,
  emptyLabel = "Every task",
  includePrefix = false,
  allowNone = true,
}: RecurrencePickerProps) {
  const [open, setOpen] = useState(false);
  const [editor, setEditor] = useState<RecurrenceEditorState>(() => recurrenceEditorState(value));
  const { panel, mode, weekdays, interval, monthlyPattern, monthDay, monthlyOrdinal, monthlyWeekday, advanced } = editor;

  const updateEditor = (patch: Partial<RecurrenceEditorState>) => {
    setEditor((current) => ({ ...current, ...patch }));
  };

  const openEditor = () => {
    setEditor(recurrenceEditorState(value));
    setOpen(true);
  };

  const friendlyValue = useMemo(() => {
    if (mode === "none") return null;
    return buildRecurrenceRrule({
      frequency: mode.toUpperCase() as RecurrenceFrequency,
      interval: mode === "weekly" ? interval : 1,
      byday: mode === "weekly" ? weekdays : undefined,
      bymonthday: mode === "monthly" && monthlyPattern === "monthday" ? monthDay : null,
      monthlyOrdinalWeekday: mode === "monthly" && monthlyPattern === "ordinal"
        ? { ordinal: monthlyOrdinal, weekday: monthlyWeekday }
        : null,
    }, { includePrefix });
  }, [includePrefix, interval, mode, monthDay, monthlyOrdinal, monthlyPattern, monthlyWeekday, weekdays]);

  const activeValue = panel === "friendly" ? friendlyValue : advanced;
  const parsedActive = parseRecurrenceRrule(activeValue);
  const previewSupported = Boolean(parsedActive.parts && parsedActive.parts.unsupported.length === 0);
  const preview = parsedActive.valid && previewSupported ? recurrencePreview(activeValue) : [];
  const advancedError = panel === "advanced" ? parsedActive.error : null;
  const friendlyError = panel === "friendly" && mode === "weekly" && weekdays.length === 0
    ? "Choose at least one weekday."
    : null;
  const canApply = !disabled && !friendlyError && (panel === "friendly" || parsedActive.valid);
  const summary = recurrenceSummary(value, { emptyLabel });

  const apply = (event: FormEvent) => {
    event.preventDefault();
    if (!canApply) return;
    const next = panel === "friendly"
      ? friendlyValue
      : normalizeRecurrenceRrule(advanced, { includePrefix });
    onChange(next);
    setOpen(false);
  };

  return (
    <div className="recurrence-picker">
      <button
        type="button"
        className="recurrence-picker__trigger"
        disabled={disabled}
        aria-label={triggerLabel ?? label}
        onClick={openEditor}
      >
        <Repeat2 size={15} aria-hidden="true" />
        <span className="recurrence-picker__trigger-text">{summary}</span>
      </button>

      <FormModal
        open={open}
        title="Recurrence"
        eyebrow={label}
        subtitle="Choose a common pattern or keep a raw RRULE for edge cases."
        onClose={() => setOpen(false)}
        onSubmit={apply}
        width="narrow"
        formClassName="recurrence-picker__form"
        bodyClassName="recurrence-picker__body"
        actions={(
          <>
            <button type="button" className="btn btn--ghost" onClick={() => setOpen(false)}>Cancel</button>
            <button type="submit" className="btn btn--moss" disabled={!canApply}>Apply</button>
          </>
        )}
      >
        <div className="recurrence-picker__tabs" aria-label="Recurrence editor mode">
          <button
            type="button"
            className={panel === "friendly" ? "recurrence-picker__tab is-active" : "recurrence-picker__tab"}
            aria-pressed={panel === "friendly"}
            onClick={() => updateEditor({ panel: "friendly" })}
          >
            <CalendarClock size={15} aria-hidden="true" />
            Friendly
          </button>
          <button
            type="button"
            className={panel === "advanced" ? "recurrence-picker__tab is-active" : "recurrence-picker__tab"}
            aria-pressed={panel === "advanced"}
            onClick={() => updateEditor({ panel: "advanced" })}
          >
            <Code2 size={15} aria-hidden="true" />
            Advanced
          </button>
        </div>

        {panel === "friendly" ? (
          <FriendlyRecurrenceFields
            allowNone={allowNone}
            mode={mode}
            weekdays={weekdays}
            interval={interval}
            monthlyPattern={monthlyPattern}
            monthDay={monthDay}
            monthlyOrdinal={monthlyOrdinal}
            monthlyWeekday={monthlyWeekday}
            error={friendlyError}
            onModeChange={(nextMode) => updateEditor({ mode: nextMode })}
            onWeekdaysChange={(nextWeekdays) => updateEditor({ weekdays: nextWeekdays })}
            onIntervalChange={(nextInterval) => updateEditor({ interval: nextInterval })}
            onMonthlyPatternChange={(nextMonthlyPattern) => updateEditor({ monthlyPattern: nextMonthlyPattern })}
            onMonthDayChange={(nextMonthDay) => updateEditor({ monthDay: nextMonthDay })}
            onMonthlyOrdinalChange={(nextMonthlyOrdinal) => updateEditor({ monthlyOrdinal: nextMonthlyOrdinal })}
            onMonthlyWeekdayChange={(nextMonthlyWeekday) => updateEditor({ monthlyWeekday: nextMonthlyWeekday })}
          />
        ) : (
          <AdvancedRecurrenceField
            value={advanced}
            error={advancedError}
            onChange={(nextAdvanced) => updateEditor({ advanced: nextAdvanced })}
          />
        )}

        <RecurrencePreview
          value={activeValue}
          preview={preview}
          valid={parsedActive.valid}
          previewSupported={previewSupported}
        />
      </FormModal>
    </div>
  );
}

function FriendlyRecurrenceFields({
  allowNone,
  mode,
  weekdays,
  interval,
  monthlyPattern,
  monthDay,
  monthlyOrdinal,
  monthlyWeekday,
  error,
  onModeChange,
  onWeekdaysChange,
  onIntervalChange,
  onMonthlyPatternChange,
  onMonthDayChange,
  onMonthlyOrdinalChange,
  onMonthlyWeekdayChange,
}: {
  allowNone: boolean;
  mode: FriendlyMode;
  weekdays: RecurrenceWeekday[];
  interval: number;
  monthlyPattern: MonthlyPattern;
  monthDay: number;
  monthlyOrdinal: RecurrenceMonthlyOrdinal;
  monthlyWeekday: RecurrenceWeekday;
  error: string | null;
  onModeChange: (value: FriendlyMode) => void;
  onWeekdaysChange: (value: RecurrenceWeekday[]) => void;
  onIntervalChange: (value: number) => void;
  onMonthlyPatternChange: (value: MonthlyPattern) => void;
  onMonthDayChange: (value: number) => void;
  onMonthlyOrdinalChange: (value: RecurrenceMonthlyOrdinal) => void;
  onMonthlyWeekdayChange: (value: RecurrenceWeekday) => void;
}) {
  const options = allowNone ? FRIENDLY_OPTIONS : FRIENDLY_OPTIONS.filter((option) => option.value !== "none");
  return (
    <section className="recurrence-picker__section" aria-label="Friendly recurrence">
      <FormModalGrid>
        <FormField label="Repeats" requirement="required" className="form-modal__field recurrence-picker__field">
          <select
            className="input recurrence-picker__select"
            aria-label="Repeats"
            value={mode}
            onChange={(event) => onModeChange(event.currentTarget.value as FriendlyMode)}
          >
            {options.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </FormField>

        {mode === "weekly" ? (
          <FormField label="Every" requirement="required" className="form-modal__field recurrence-picker__field">
            <div className="recurrence-picker__stepper">
              <input
                className="input recurrence-picker__number"
                type="number"
                min={1}
                max={52}
                value={interval}
                aria-label="Every weeks"
                onChange={(event) => onIntervalChange(clampNumber(event.currentTarget.value, 1, 52))}
              />
              <span>week{interval === 1 ? "" : "s"}</span>
            </div>
          </FormField>
        ) : null}
      </FormModalGrid>

      {mode === "weekly" ? (
        <fieldset className="recurrence-picker__weekday-set" aria-describedby={error ? "recurrence-weekday-error" : undefined}>
          <legend>Weekdays</legend>
          <div className="recurrence-picker__weekday-options">
            {RECURRENCE_WEEKDAYS.map((day) => {
              const selected = weekdays.includes(day.value);
              return (
                <button
                  key={day.value}
                  type="button"
                  className={selected ? "recurrence-picker__weekday is-selected" : "recurrence-picker__weekday"}
                  aria-pressed={selected}
                  onClick={() => onWeekdaysChange(toggleWeekday(weekdays, day.value))}
                >
                  {day.shortLabel}
                </button>
              );
            })}
          </div>
          {error ? <p id="recurrence-weekday-error" className="recurrence-picker__error">{error}</p> : null}
        </fieldset>
      ) : null}

      {mode === "monthly" ? (
        <div className="recurrence-picker__monthly-fields">
          <FormField label="Monthly pattern" requirement="required" className="form-modal__field recurrence-picker__field">
            <select
              className="input recurrence-picker__select"
              aria-label="Monthly pattern"
              value={monthlyPattern}
              onChange={(event) => onMonthlyPatternChange(event.currentTarget.value as MonthlyPattern)}
            >
              <option value="monthday">Day of month</option>
              <option value="ordinal">Weekday position</option>
            </select>
          </FormField>

          {monthlyPattern === "monthday" ? (
            <FormField label="Day of month" requirement="required" className="form-modal__field recurrence-picker__field">
              <input
                className="input recurrence-picker__number"
                type="number"
                min={1}
                max={31}
                value={monthDay}
                aria-label="Day of month"
                onChange={(event) => onMonthDayChange(clampNumber(event.currentTarget.value, 1, 31))}
              />
            </FormField>
          ) : (
            <div className="recurrence-picker__ordinal-fields">
              <FormField label="Position" requirement="required" className="form-modal__field recurrence-picker__field">
                <select
                  className="input recurrence-picker__select"
                  aria-label="Position"
                  value={monthlyOrdinal}
                  onChange={(event) => onMonthlyOrdinalChange(parseMonthlyOrdinal(event.currentTarget.value))}
                >
                  {MONTHLY_ORDINAL_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </FormField>
              <FormField label="Weekday" requirement="required" className="form-modal__field recurrence-picker__field">
                <select
                  className="input recurrence-picker__select"
                  aria-label="Weekday"
                  value={monthlyWeekday}
                  onChange={(event) => onMonthlyWeekdayChange(event.currentTarget.value as RecurrenceWeekday)}
                >
                  {RECURRENCE_WEEKDAYS.map((day) => (
                    <option key={day.value} value={day.value}>{day.label}</option>
                  ))}
                </select>
              </FormField>
            </div>
          )}
        </div>
      ) : null}
    </section>
  );
}

function AdvancedRecurrenceField({
  value,
  error,
  onChange,
}: {
  value: string;
  error: string | null;
  onChange: (value: string) => void;
}) {
  return (
    <FormField
      label="Raw RRULE"
      requirement="required"
      className="form-modal__field recurrence-picker__field"
      helpText="Examples: FREQ=WEEKLY;BYDAY=MO,TH or RRULE:FREQ=MONTHLY;BYMONTHDAY=1."
    >
      <textarea
        className="input recurrence-picker__raw"
        value={value}
        aria-label="Raw RRULE"
        aria-invalid={Boolean(error)}
        aria-describedby={error ? "recurrence-advanced-error" : undefined}
        onChange={(event) => onChange(event.currentTarget.value)}
      />
      {error ? <p id="recurrence-advanced-error" className="recurrence-picker__error">{error}</p> : null}
    </FormField>
  );
}

function RecurrencePreview({
  value,
  preview,
  valid,
  previewSupported,
}: {
  value: string | null;
  preview: string[];
  valid: boolean;
  previewSupported: boolean;
}) {
  if (!value) {
    return <p className="recurrence-picker__preview recurrence-picker__preview--empty">This item appears on every generated task.</p>;
  }
  if (!valid) {
    return <p className="recurrence-picker__preview recurrence-picker__preview--error">Fix the RRULE to preview upcoming dates.</p>;
  }
  if (!previewSupported) {
    return (
      <p className="recurrence-picker__preview recurrence-picker__preview--empty">
        Preview is available for frequency, interval, weekday, month-day, count, and until rules.
      </p>
    );
  }
  if (preview.length === 0) {
    return <p className="recurrence-picker__preview recurrence-picker__preview--empty">No upcoming occurrences in the preview window.</p>;
  }
  return (
    <section className="recurrence-picker__preview" aria-label="Next occurrences">
      <span className="recurrence-picker__preview-label">Next occurrences</span>
      <ol>
        {preview.map((item) => <li key={item}>{item}</li>)}
      </ol>
    </section>
  );
}

function toggleWeekday(days: readonly RecurrenceWeekday[], day: RecurrenceWeekday): RecurrenceWeekday[] {
  if (days.includes(day)) return days.filter((candidate) => candidate !== day);
  const selectedDays = new Set(days);
  const next: RecurrenceWeekday[] = [];
  for (const candidate of RECURRENCE_WEEKDAYS) {
    if (candidate.value === day || selectedDays.has(candidate.value)) {
      next.push(candidate.value);
    }
  }
  return next;
}

function clampNumber(value: string, min: number, max: number): number {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return min;
  return Math.min(Math.max(parsed, min), max);
}

function parseMonthlyOrdinal(value: string): RecurrenceMonthlyOrdinal {
  const parsed = Number.parseInt(value, 10);
  return parsed === -1 || parsed >= 1 && parsed <= 4 ? parsed as RecurrenceMonthlyOrdinal : 1;
}
