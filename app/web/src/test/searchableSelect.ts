import { fireEvent, screen, within } from "@testing-library/react";

export async function chooseSearchableOption(
  container: HTMLElement,
  label: RegExp,
  optionName: RegExp,
): Promise<void> {
  const input = await within(container).findByRole("combobox", { name: label });
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: optionName.source.replace(/\\/g, "") } });
  fireEvent.mouseDown(await screen.findByRole("option", { name: optionName }));
}
