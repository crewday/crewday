import { fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useCloseOnEscape } from "./useCloseOnEscape";

function Harness({
  active = true,
  onClose,
}: {
  active?: boolean;
  onClose: () => void;
}) {
  useCloseOnEscape(onClose, active);
  return <button type="button">Focusable</button>;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useCloseOnEscape", () => {
  it("closes on Escape and removes the keydown listener on unmount", () => {
    const add = vi.spyOn(window, "addEventListener");
    const remove = vi.spyOn(window, "removeEventListener");
    const onClose = vi.fn();

    const { unmount } = render(<Harness onClose={onClose} />);

    fireEvent.keyDown(window, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(add).toHaveBeenCalledWith("keydown", expect.any(Function));

    const handler = add.mock.calls.find((call) => call[0] === "keydown")?.[1];
    unmount();

    expect(remove).toHaveBeenCalledWith("keydown", handler);
  });

  it("stays inactive when disabled", () => {
    const onClose = vi.fn();

    render(<Harness active={false} onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });

    expect(onClose).not.toHaveBeenCalled();
  });

  it("only closes the most recently mounted active drawer", () => {
    const parentClose = vi.fn();
    const childClose = vi.fn();

    const { rerender } = render(
      <>
        <Harness onClose={parentClose} />
        <Harness onClose={childClose} />
      </>,
    );

    fireEvent.keyDown(window, { key: "Escape" });

    expect(childClose).toHaveBeenCalledTimes(1);
    expect(parentClose).not.toHaveBeenCalled();

    rerender(<Harness onClose={parentClose} />);
    fireEvent.keyDown(window, { key: "Escape" });

    expect(parentClose).toHaveBeenCalledTimes(1);
  });

  it("lets native open dialogs handle Escape", () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);

    const dialog = document.createElement("dialog");
    dialog.setAttribute("open", "");
    const button = document.createElement("button");
    dialog.append(button);
    document.body.append(dialog);
    try {
      fireEvent.keyDown(button, { key: "Escape", bubbles: true });
    } finally {
      dialog.remove();
    }

    expect(onClose).not.toHaveBeenCalled();
  });
});
