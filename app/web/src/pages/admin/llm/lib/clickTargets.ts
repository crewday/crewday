import type { MouseEvent } from "react";

export function shouldOpenGraphEditor(event: MouseEvent<HTMLElement>): boolean {
  const target = event.target;
  if (event.detail === 0 && event.currentTarget === document.activeElement) return true;
  return (
    target instanceof Element &&
    Boolean(target.closest("[data-llm-edit-target='true']"))
  );
}
