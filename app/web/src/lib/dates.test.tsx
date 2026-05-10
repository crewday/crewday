import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import DateTime from "@/components/DateTime";
import {
  formatAbsoluteDateTime,
  formatDateTimeDisplay,
  formatExactDateTime,
  formatRelativeDateTime,
} from "@/lib/dates";

const now = "2026-07-15T12:00:00.000Z";

describe("date-time formatting", () => {
  it("formats recent past timestamps relatively", () => {
    expect(
      formatRelativeDateTime("2026-07-15T11:00:00.000Z", {
        now,
        locale: "en-US",
      }),
    ).toBe("1 hour ago");
    expect(
      formatRelativeDateTime("2026-07-15T11:40:00.000Z", {
        now,
        locale: "en-US",
      }),
    ).toBe("20 minutes ago");
    expect(
      formatRelativeDateTime("2026-07-11T12:00:00.000Z", {
        now,
        locale: "en-US",
      }),
    ).toBe("4 days ago");
  });

  it("formats recent future timestamps relatively", () => {
    expect(
      formatRelativeDateTime("2026-07-15T12:15:00.000Z", {
        now,
        locale: "en-US",
      }),
    ).toBe("in 15 minutes");
  });

  it("rolls relative labels over at hour and day boundaries", () => {
    expect(
      formatRelativeDateTime("2026-07-15T11:00:30.000Z", {
        now,
        locale: "en-US",
      }),
    ).toBe("1 hour ago");
    expect(
      formatRelativeDateTime("2026-07-14T12:30:00.000Z", {
        now,
        locale: "en-US",
      }),
    ).toBe("1 day ago");
  });

  it("switches to absolute labels outside the relative window", () => {
    expect(
      formatDateTimeDisplay("2026-07-07T12:00:00.000Z", {
        now,
        locale: "en-US",
        timeZone: "UTC",
      }),
    ).toMatchObject({
      label: "July 7th, 2026",
      isRelative: false,
    });
  });

  it("formats absolute English ordinals with optional time", () => {
    expect(
      formatAbsoluteDateTime("2026-07-15T09:30:00.000Z", {
        locale: "en-US",
        timeZone: "UTC",
      }),
    ).toBe("July 15th, 2026");
    expect(
      formatAbsoluteDateTime("2026-07-15T09:30:00.000Z", {
        locale: "en-US",
        timeZone: "UTC",
        showTime: true,
      }),
    ).toBe("July 15th, 2026, 09:30 AM");
  });

  it("keeps locale and timezone seams for exact and absolute formatting", () => {
    expect(
      formatAbsoluteDateTime("2026-07-15T23:30:00.000Z", {
        locale: "fr-FR",
        timeZone: "Europe/Paris",
      }),
    ).toBe("16 juillet 2026");
    expect(
      formatExactDateTime("2026-07-15T23:30:00.000Z", {
        locale: "en-US",
        timeZone: "America/New_York",
      }),
    ).toBe("July 15, 2026 at 7:30 PM");
  });

  it("returns null for invalid or empty values", () => {
    expect(formatDateTimeDisplay(null)).toBeNull();
    expect(formatDateTimeDisplay("not-a-date")).toBeNull();
  });

  it("falls back instead of throwing for invalid Intl options", () => {
    expect(
      formatDateTimeDisplay("2026-07-07T12:00:00.000Z", {
        locale: "not_a_locale",
        timeZone: "No/Such_Zone",
      }),
    ).toMatchObject({
      dateTime: "2026-07-07T12:00:00.000Z",
      isRelative: false,
    });
    expect(
      formatRelativeDateTime("2026-07-15T12:15:00.000Z", {
        now,
        locale: "not_a_locale",
      }),
    ).toBe("in 15 minutes");
  });
});

describe("DateTime", () => {
  it("renders a semantic time element with exact attributes", () => {
    render(
      <DateTime
        value="2026-07-07T12:00:00.000Z"
        locale="en-US"
        timeZone="UTC"
        relativeWithinDays={1}
      />,
    );

    const time = screen.getByText("July 7th, 2026");
    expect(time.tagName).toBe("TIME");
    expect(time).toHaveAttribute("dateTime", "2026-07-07T12:00:00.000Z");
    expect(time).toHaveAttribute("title", "July 7, 2026 at 12:00 PM");
  });

  it("renders the configured fallback for null or invalid values", () => {
    const { rerender } = render(<DateTime value={null} empty="No date" />);
    expect(screen.getByText("No date")).toBeInTheDocument();

    rerender(<DateTime value="not-a-date" empty="No date" />);
    expect(screen.getByText("No date")).toBeInTheDocument();
  });
});
