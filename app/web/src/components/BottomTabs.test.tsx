import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import BottomTabs from "./BottomTabs";

describe("<BottomTabs>", () => {
  it("keeps Chat as the phone full-screen chat route", () => {
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <BottomTabs />
      </MemoryRouter>,
    );

    const nav = screen.getByRole("navigation", { name: "Bottom navigation" });
    const chat = within(nav).getByRole("link", { name: /Chat/i });
    expect(chat).toHaveAttribute("href", "/chat");
    expect(chat).toHaveClass("tab--active");
  });

  it("keeps the bottom tabs hidden at desktop widths", () => {
    const shellCss = readFileSync(resolve(__dirname, "../styles/shell.css"), "utf8");

    expect(shellCss).toMatch(/@media\s*\(min-width:\s*720px\)\s*{[\s\S]*\.phone__tabs\s*{\s*display:\s*none;\s*}/);
  });
});
