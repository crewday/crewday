import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ASSET_ICON_NAMES } from "./AssetIcon";
import IconSelector from "./IconSelector";

describe("IconSelector", () => {
  it("uses an immutable deterministic whitelist", () => {
    expect(Object.isFrozen(ASSET_ICON_NAMES)).toBe(true);
    expect(ASSET_ICON_NAMES).toEqual([...ASSET_ICON_NAMES].sort());
  });

  it("selects a whitelisted Lucide icon and reports its PascalCase name", () => {
    const onChange = vi.fn();
    render(<IconSelector label="Icon" value="" onChange={onChange} />);

    fireEvent.change(screen.getByLabelText("Search icon choices"), {
      target: { value: "ladder" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Select Waves Ladder icon" }));

    expect(onChange).toHaveBeenCalledWith("WavesLadder");
    expect(screen.queryByRole("button", { name: "Select Chef Hat icon" })).not.toBeInTheDocument();
  });

  it("shows the selected icon preview and clears optional values", () => {
    const onChange = vi.fn();
    render(<IconSelector label="Icon" value="ChefHat" onChange={onChange} />);

    expect(screen.getByText("Selected icon")).toBeInTheDocument();
    expect(screen.getAllByText("Chef Hat").length).toBeGreaterThan(0);

    const choices = screen.getByRole("group", { name: "Icon choices" });
    fireEvent.click(within(choices).getByRole("button", { name: "No icon" }));

    expect(onChange).toHaveBeenCalledWith("");
  });

  it("does not expose unknown stored icon names as selectable values", () => {
    render(<IconSelector label="Icon" value="LegacyRoleIcon" onChange={vi.fn()} />);

    expect(screen.getAllByText("No icon").length).toBeGreaterThan(0);
    expect(screen.getByText(/Saved icon is unavailable/)).toBeInTheDocument();
    expect(screen.queryByText("LegacyRoleIcon")).not.toBeInTheDocument();
  });
});
