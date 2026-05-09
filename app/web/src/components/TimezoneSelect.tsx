import { useMemo } from "react";
import SearchableSelect, { type SearchableSelectOption } from "@/components/SearchableSelect";

interface TimezoneSelectProps {
  value: string;
  onChange: (value: string) => void;
  required?: boolean;
}

const FALLBACK_TIMEZONES = [
  "UTC",
  "Africa/Cairo",
  "Africa/Johannesburg",
  "America/Anchorage",
  "America/Argentina/Buenos_Aires",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "America/Mexico_City",
  "America/New_York",
  "America/Phoenix",
  "America/Sao_Paulo",
  "America/Toronto",
  "Asia/Bangkok",
  "Asia/Dubai",
  "Asia/Hong_Kong",
  "Asia/Kolkata",
  "Asia/Manila",
  "Asia/Seoul",
  "Asia/Shanghai",
  "Asia/Singapore",
  "Asia/Tokyo",
  "Australia/Melbourne",
  "Australia/Sydney",
  "Europe/Amsterdam",
  "Europe/Berlin",
  "Europe/Lisbon",
  "Europe/London",
  "Europe/Madrid",
  "Europe/Paris",
  "Pacific/Auckland",
  "Pacific/Honolulu",
] as const;

type IntlWithSupportedValues = typeof Intl & {
  supportedValuesOf?: (key: "timeZone") => string[];
};

export default function TimezoneSelect({ value, onChange, required = false }: TimezoneSelectProps) {
  const options = useMemo(timezoneOptions, []);
  return (
    <SearchableSelect
      label="Timezone"
      value={value}
      options={options}
      onChange={onChange}
      required={required}
      placeholder="Search timezone"
      noResultsLabel="No timezones found"
    />
  );
}

function timezoneOptions(): SearchableSelectOption[] {
  return supportedTimezones().map((zone) => ({
    value: zone,
    label: zone,
    searchText: zone.replace(/[_/]/g, " "),
  }));
}

function supportedTimezones(): string[] {
  const supportedValuesOf = (Intl as IntlWithSupportedValues).supportedValuesOf;
  const platformZones = supportedValuesOf?.("timeZone") ?? [];
  return [...new Set(["UTC", ...platformZones, ...FALLBACK_TIMEZONES])].sort((a, b) => a.localeCompare(b));
}
