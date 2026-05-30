import { useEffect, useMemo, useState, type FormEvent } from "react";
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
  type RecurrenceWeekday,
} from "@/components/recurrence";

type RecurrencePickerPanel = "friendly" | "advanced";
type FriendlyMode = "none" | Lowercase<RecurrenceFrequency>;

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
  const [panel, setPanel] = useState<RecurrencePickerPanel>("friendly");
  const [mode, setMode] = useState<FriendlyMode>("none");
  const [weekdays, setWeekdays] = useState<RecurrenceWeekday[]>(["MO"]);
  const [interval, setInterval] = useState(1);
  const [monthDay, setMonthDay] = useState(1);
  const [advanced, setAdvanced] = useState("");

  useEffect(() => {
    if (!open) return;
    const parsed = parseRecurrenceRrule(value);
    const frequency = frequencyFromRecurrence(value).toLowerCase() as FriendlyMode;
    setPanel(!parsed.value || parsed.valid && parsed.parts?.unsupported.length === 0 ? "friendly" : "advanced");
    setMode(parsed.value ? frequency : "none");
    setWeekdays(parsed.parts?.byday.length ? parsed.parts.byday : ["MO"]);
    setInterval(parsed.parts?.interval ?? 1);
    setMonthDay(parsed.parts?.bymonthday ?? 1);
    setAdvanced(value ?? "");
  }, [open, value]);

  const friendlyValue = useMemo(() => {
    if (mode === "none") return null;
    return buildRecurrenceRrule({
      frequency: mode.toUpperCase() as RecurrenceFrequency,
      interval: mode === "weekly" ? interval : 1,
      byday: mode === "weekly" ? weekdays : undefined,
      bymonthday: mode === "monthly" ? monthDay : null,
    }, { includePrefix });
  }, [includePrefix, interval, mode, monthDay, weekdays]);

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
        onClick={() => setOpen(true)}
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
        <div className="recurrence-picker__tabs" role="group" aria-label="Recurrence editor mode">
          <button
            type="button"
            className={panel === "friendly" ? "recurrence-picker__tab is-active" : "recurrence-picker__tab"}
            aria-pressed={panel === "friendly"}
            onClick={() => setPanel("friendly")}
          >
            <CalendarClock size={15} aria-hidden="true" />
            Friendly
          </button>
          <button
            type="button"
            className={panel === "advanced" ? "recurrence-picker__tab is-active" : "recurrence-picker__tab"}
            aria-pressed={panel === "advanced"}
            onClick={() => setPanel("advanced")}
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
            monthDay={monthDay}
            error={friendlyError}
            onModeChange={setMode}
            onWeekdaysChange={setWeekdays}
            onIntervalChange={setInterval}
            onMonthDayChange={setMonthDay}
          />
        ) : (
          <AdvancedRecurrenceField
            value={advanced}
            error={advancedError}
            onChange={setAdvanced}
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
  monthDay,
  error,
  onModeChange,
  onWeekdaysChange,
  onIntervalChange,
  onMonthDayChange,
}: {
  allowNone: boolean;
  mode: FriendlyMode;
  weekdays: RecurrenceWeekday[];
  interval: number;
  monthDay: number;
  error: string | null;
  onModeChange: (value: FriendlyMode) => void;
  onWeekdaysChange: (value: RecurrenceWeekday[]) => void;
  onIntervalChange: (value: number) => void;
  onMonthDayChange: (value: number) => void;
}) {
  const options = allowNone ? FRIENDLY_OPTIONS : FRIENDLY_OPTIONS.filter((option) => option.value !== "none");
  return (
    <section className="recurrence-picker__section" aria-label="Friendly recurrence">
      <FormModalGrid>
        <FormField label="Repeats" requirement="required" className="form-modal__field recurrence-picker__field">
          <select
            className="input recurrence-picker__select"
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
        <FormField label="Day of month" requirement="required" className="form-modal__field recurrence-picker__field">
          <input
            className="input recurrence-picker__number"
            type="number"
            min={1}
            max={31}
            value={monthDay}
            onChange={(event) => onMonthDayChange(clampNumber(event.currentTarget.value, 1, 31))}
          />
        </FormField>
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
  return RECURRENCE_WEEKDAYS
    .map((candidate) => candidate.value)
    .filter((candidate) => candidate === day || days.includes(candidate));
}

function clampNumber(value: string, min: number, max: number): number {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return min;
  return Math.min(Math.max(parsed, min), max);
}
