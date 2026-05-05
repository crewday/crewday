import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import type { Property } from "@/types/api";
import ConfidenceChip from "./ConfidenceChip";
import { CATEGORIES, confidenceClass } from "./lib/expenseHelpers";
import type { FieldConfidences, FieldValues } from "./lib/scanDerivation";

interface SubmitExpenseFieldsProps {
  values: FieldValues;
  confidences: FieldConfidences;
  isScanned: boolean;
  properties: Property[];
  onFieldChange: <K extends keyof FieldValues>(key: K, value: FieldValues[K]) => void;
}

export function SubmitExpenseFields(props: SubmitExpenseFieldsProps) {
  // code-health: ignore[nloc] Declarative form field layout is extracted from SubmitExpenseForm and has little branching.
  const { values, confidences, isScanned, properties, onFieldChange } = props;
  return (
    <>
      <TextField
        name="vendor"
        label="Vendor"
        placeholder="e.g. Carrefour"
        required
        value={values.vendor}
        confidence={confidences.vendor}
        isScanned={isScanned}
        onChange={(value) => onFieldChange("vendor", value)}
      />
      <div className="form__row">
        <TextField
          name="amount"
          label="Amount"
          type="number"
          step="0.01"
          placeholder="0.00"
          required
          grow
          value={values.amount}
          confidence={confidences.amount}
          isScanned={isScanned}
          onChange={(value) => onFieldChange("amount", value)}
        />
        <label className="field field--currency">
          <span>Currency</span>
          <input
            name="currency"
            value={values.currency}
            onChange={(e) => onFieldChange("currency", e.target.value)}
          />
        </label>
      </div>
      <TextField
        name="purchased_on"
        label="Purchase date"
        type="date"
        required
        value={values.purchased_on}
        confidence={confidences.purchased_on}
        isScanned={isScanned}
        onChange={(value) => onFieldChange("purchased_on", value)}
      />
      <PropertyField
        value={values.property_id}
        properties={properties}
        onChange={(value) => onFieldChange("property_id", value)}
      />
      <CategoryField
        value={values.category}
        confidence={confidences.category}
        isScanned={isScanned}
        onChange={(value) => onFieldChange("category", value)}
      />
      <label className={`field ${confidenceClass(confidences.note_md)}`}>
        <span>
          Note
          <ConfidenceChip isScanned={isScanned} confidence={confidences.note_md} />
        </span>
        <AutoGrowTextarea
          name="note_md"
          placeholder="What it was for"
          value={values.note_md}
          onChange={(e) => onFieldChange("note_md", e.target.value)}
        />
      </label>
    </>
  );
}

interface TextFieldProps {
  name: string;
  label: string;
  value: string;
  confidence: number | null;
  isScanned: boolean;
  onChange: (value: string) => void;
  type?: string;
  step?: string;
  placeholder?: string;
  required?: boolean;
  grow?: boolean;
}

function TextField(props: TextFieldProps) {
  const {
    name,
    label,
    value,
    confidence,
    isScanned,
    onChange,
    type,
    step,
    placeholder,
    required,
    grow,
  } = props;
  return (
    <label className={`field ${grow ? "field--grow " : ""}${confidenceClass(confidence)}`}>
      <span>
        {label}
        <ConfidenceChip isScanned={isScanned} confidence={confidence} />
      </span>
      <input
        name={name}
        type={type}
        step={step}
        placeholder={placeholder}
        required={required}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

function PropertyField({
  value,
  properties,
  onChange,
}: {
  value: string;
  properties: Property[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="field">
      <span>Property (optional)</span>
      <select
        name="property_id"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">— No property —</option>
        {properties.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
    </label>
  );
}

function CategoryField({
  value,
  confidence,
  isScanned,
  onChange,
}: {
  value: FieldValues["category"];
  confidence: number | null;
  isScanned: boolean;
  onChange: (value: FieldValues["category"]) => void;
}) {
  return (
    <div className={`field ${confidenceClass(confidence)}`}>
      <span>
        Category
        <ConfidenceChip isScanned={isScanned} confidence={confidence} />
      </span>
      <div className="chip-group">
        {CATEGORIES.map((c) => (
          <label key={c.value} className="chip-radio">
            <input
              type="radio"
              name="category"
              value={c.value}
              checked={value === c.value}
              onChange={() => onChange(c.value)}
            />
            <span>{c.label}</span>
          </label>
        ))}
      </div>
    </div>
  );
}
