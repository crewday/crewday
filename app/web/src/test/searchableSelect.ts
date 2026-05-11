import { fireEvent, within } from "@testing-library/react";

export async function chooseSearchableOption(
  container: HTMLElement,
  label: RegExp,
  optionName: RegExp,
): Promise<void> {
  const input = await within(container).findByRole("combobox", { name: label });
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: optionName.source.replace(/\\/g, "") } });
  fireEvent.mouseDown(await within(container).findByRole("option", { name: optionName }));
}
