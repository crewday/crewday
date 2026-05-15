import { useState } from "react";
import type { DragEvent, DragEventHandler } from "react";

export type ReorderDropPosition = "before" | "after";

export interface ReorderDropTarget {
  id: string;
  index: number;
  position: ReorderDropPosition;
}

interface UseReorderableListArgs<T> {
  items: readonly T[];
  getId: (item: T) => string;
  onMove: (id: string, toIndex: number) => void;
  disabled?: boolean;
  defaultDropPosition?: ReorderDropPosition;
}

interface ReorderableItemProps {
  draggable: boolean;
  onDragStart: DragEventHandler<HTMLElement>;
  onDragOver: DragEventHandler<HTMLElement>;
  onDragLeave: DragEventHandler<HTMLElement>;
  onDrop: DragEventHandler<HTMLElement>;
  onDragEnd: DragEventHandler<HTMLElement>;
}

interface ReorderableListProps {
  onDragLeave: DragEventHandler<HTMLElement>;
  onDrop: DragEventHandler<HTMLElement>;
}

export interface UseReorderableListResult {
  draggedId: string | null;
  dropTarget: ReorderDropTarget | null;
  getItemProps: (index: number) => ReorderableItemProps;
  getListProps: () => ReorderableListProps;
  clearDragState: () => void;
}

function dropPosition(
  event: DragEvent<HTMLElement>,
  fallback: ReorderDropPosition,
): ReorderDropPosition {
  const rect = event.currentTarget.getBoundingClientRect();
  if (rect.height <= 0) return fallback;
  return event.clientY < rect.top + rect.height / 2 ? "before" : "after";
}

function finalIndex(fromIndex: number, target: ReorderDropTarget): number {
  const insertionIndex = target.index + (target.position === "after" ? 1 : 0);
  return fromIndex < insertionIndex ? insertionIndex - 1 : insertionIndex;
}

export function useReorderableList<T>({
  items,
  getId,
  onMove,
  disabled = false,
  defaultDropPosition = "after",
}: UseReorderableListArgs<T>): UseReorderableListResult {
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<ReorderDropTarget | null>(null);

  function clearDragState(): void {
    setDraggedId(null);
    setDropTarget(null);
  }

  function getItemProps(index: number): ReorderableItemProps {
    const item = items[index];
    const id = item ? getId(item) : "";

    return {
      draggable: !disabled,
      onDragStart(event) {
        if (disabled || !id) return;
        setDraggedId(id);
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", id);
      },
      onDragOver(event) {
        if (disabled || draggedId === null || draggedId === id || !id) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        const nextTarget = { id, index, position: dropPosition(event, defaultDropPosition) };
        setDropTarget((current) =>
          current?.id === nextTarget.id &&
          current.index === nextTarget.index &&
          current.position === nextTarget.position
            ? current
            : nextTarget,
        );
      },
      onDragLeave(event) {
        if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
        setDropTarget((current) => (current?.id === id ? null : current));
      },
      onDrop(event) {
        if (disabled || draggedId === null || !id) return;
        event.preventDefault();
        const fromIndex = items.findIndex((candidate) => getId(candidate) === draggedId);
        const target =
          dropTarget?.id === id
            ? dropTarget
            : { id, index, position: dropPosition(event, defaultDropPosition) };
        if (fromIndex >= 0 && draggedId !== id) {
          const toIndex = finalIndex(fromIndex, target);
          if (toIndex !== fromIndex) onMove(draggedId, toIndex);
        }
        clearDragState();
      },
      onDragEnd: clearDragState,
    };
  }

  function getListProps(): ReorderableListProps {
    return {
      onDragLeave(event) {
        if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
        setDropTarget(null);
      },
      onDrop() {
        clearDragState();
      },
    };
  }

  return {
    draggedId,
    dropTarget,
    getItemProps,
    getListProps,
    clearDragState,
  };
}
