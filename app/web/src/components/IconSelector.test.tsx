import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ASSET_ICON_NAMES, isAssetIconName } from "./AssetIcon.registry";
import IconSelector from "./IconSelector";

describe("IconSelector", () => {
  it("uses an immutable deterministic catalog", () => {
    expect(Object.isFrozen(ASSET_ICON_NAMES)).toBe(true);
    expect(ASSET_ICON_NAMES).toEqual([...ASSET_ICON_NAMES].sort());
    expect(ASSET_ICON_NAMES.length).toBeGreaterThan(60);
  });

  it("opens a focused popover, filters icons, and reports the PascalCase name", async () => {
    const onChange = vi.fn();
    render(<IconSelector label="Icon" value="" onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Icon: No icon. Edit icon" }));
    await waitFor(() => expect(screen.getByLabelText("Search icon choices")).toHaveFocus());
    fireEvent.change(screen.getByLabelText("Search icon choices"), {
      target: { value: "ladder" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Select Waves Ladder icon" }));

    expect(onChange).toHaveBeenCalledWith("WavesLadder");
    expect(screen.queryByRole("button", { name: "Select Chef Hat icon" })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Icon choices" })).not.toBeInTheDocument();
  });

  it("shows the selected icon preview and clears optional values", () => {
    const onChange = vi.fn();
    render(<IconSelector label="Icon" value="ChefHat" onChange={onChange} />);

    expect(screen.queryByText("Selected icon")).not.toBeInTheDocument();
    expect(screen.getByText("Chef Hat")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Icon: Chef Hat. Edit icon" })).toHaveAttribute(
      "title",
      "Icon: Chef Hat. Edit icon",
    );

    fireEvent.click(screen.getByRole("button", { name: "Icon: Chef Hat. Edit icon" }));
    const choices = screen.getByRole("group", { name: "Icon choices" });
    fireEvent.click(within(choices).getByRole("button", { name: "No icon" }));

    expect(onChange).toHaveBeenCalledWith("");
    expect(screen.queryByRole("dialog", { name: "Icon choices" })).not.toBeInTheDocument();
  });

  it("does not expose unknown stored icon names as selectable values", () => {
    render(<IconSelector label="Icon" value="LegacyRoleIcon" onChange={vi.fn()} />);

    expect(screen.getByRole("button", { name: "Icon: Unknown icon. Edit icon" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Icon: Unknown icon. Edit icon" }));
    expect(screen.getByRole("button", { name: "No icon" })).toBeInTheDocument();
    expect(screen.queryByText("LegacyRoleIcon")).not.toBeInTheDocument();
  });

  it("keeps disabled selectors closed", () => {
    const onChange = vi.fn();
    render(<IconSelector label="Icon" value="" disabled onChange={onChange} />);

    const preview = screen.getByRole("button", { name: "Icon: No icon. Edit icon" });
    expect(preview).toBeDisabled();

    fireEvent.click(preview);

    expect(screen.queryByRole("dialog", { name: "Icon choices" })).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("treats object prototype names as unknown stored values", () => {
    expect(isAssetIconName("toString")).toBe(false);
    render(<IconSelector label="Icon" value="toString" onChange={vi.fn()} />);

    expect(screen.getByRole("button", { name: "Icon: Unknown icon. Edit icon" })).toBeInTheDocument();
  });

  it("supports keyboard open and close", async () => {
    render(<IconSelector label="Icon" value="Refrigerator" onChange={vi.fn()} />);

    const preview = screen.getByRole("button", { name: "Icon: Refrigerator. Edit icon" });
    preview.focus();
    fireEvent.keyDown(preview, { key: "Enter" });
    await waitFor(() => expect(screen.getByLabelText("Search icon choices")).toHaveFocus());

    fireEvent.keyDown(screen.getByLabelText("Search icon choices"), { key: "Escape" });

    expect(screen.queryByRole("dialog", { name: "Icon choices" })).not.toBeInTheDocument();
    expect(preview).toHaveFocus();
  });

  it("only stops Escape propagation while the popover is open", async () => {
    const onKeyDown = vi.fn();
    render(
      <div onKeyDown={onKeyDown}>
        <IconSelector label="Icon" value="Refrigerator" onChange={vi.fn()} />
      </div>,
    );

    const preview = screen.getByRole("button", { name: "Icon: Refrigerator. Edit icon" });
    fireEvent.keyDown(preview, { key: "Escape" });

    expect(onKeyDown).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(preview, { key: "Enter" });
    await waitFor(() => expect(screen.getByLabelText("Search icon choices")).toHaveFocus());
    onKeyDown.mockClear();

    fireEvent.keyDown(screen.getByLabelText("Search icon choices"), { key: "Escape" });

    expect(onKeyDown).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: "Icon choices" })).not.toBeInTheDocument();

    fireEvent.keyDown(preview, { key: "Escape" });

    expect(onKeyDown).toHaveBeenCalledTimes(1);
  });
});
