import { type ReactNode, useEffect, useMemo, useState } from "react";
import {
  formatDateTimeDisplay,
  parseDateTime,
  shouldRefreshRelativeDateTime,
  type DateTimeValue,
} from "@/lib/dates";

export interface DateTimeProps {
  value: DateTimeValue;
  showTime?: boolean;
  relativeWithinDays?: number;
  locale?: string;
  timeZone?: string;
  className?: string;
  empty?: ReactNode;
}

export default function DateTime({
  value,
  showTime = false,
  relativeWithinDays = 7,
  locale,
  timeZone,
  className,
  empty = null,
}: DateTimeProps) {
  const [now, setNow] = useState(() => new Date());
  const parsedValue = useMemo(() => parseDateTime(value), [value]);

  useEffect(() => {
    if (!shouldRefreshRelativeDateTime(parsedValue, now, relativeWithinDays)) return undefined;
    const id = window.setInterval(() => setNow(new Date()), 60 * 1000);
    return () => window.clearInterval(id);
  }, [parsedValue, now, relativeWithinDays]);

  const display = useMemo(
    () =>
      formatDateTimeDisplay(parsedValue, {
        now,
        showTime,
        relativeWithinDays,
        locale,
        timeZone,
      }),
    [parsedValue, now, showTime, relativeWithinDays, locale, timeZone],
  );

  if (display === null) return <>{empty}</>;

  return (
    <time className={className} dateTime={display.dateTime} title={display.title}>
      {display.label}
    </time>
  );
}
