import { describe, expect, it } from "vitest";
import { normalizeTaskDetail, updateChecklistItem } from "./taskDetailMappers";
import type { ApiTask, TaskDetailResponse } from "./taskDetailMappers";

function apiTask(overrides: Partial<ApiTask> = {}): ApiTask {
  return {
    id: "t1",
    title: "Reset guest room",
    priority: "high",
    photo_evidence: "required",
    ...overrides,
  };
}

describe("task detail mappers", () => {
  it("normalizes bare task payloads with missing optional fields", () => {
    const detail = normalizeTaskDetail(
      apiTask({
        property_id: null,
        area_id: null,
        duration_minutes: null,
        inventory_consumption_json: { linen: 2 },
        checklist: [
          { id: "ci1", text: "Check towels", checked: true, requires_photo: true },
          { id: "ci2" },
        ],
      }),
    );

    expect(detail.property).toBeNull();
    expect(detail.instructions).toEqual([]);
    expect(detail.task).toMatchObject({
      property_id: null,
      area: "",
      scheduled_start: "",
      estimated_minutes: 30,
      status: "pending",
      is_personal: false,
    });
    expect(detail.task.checklist).toEqual([
      { id: "ci1", label: "Check towels", done: true, required: true },
    ]);
    expect(detail.inventory_effects).toEqual([
      {
        item_ref: "linen",
        kind: "consume",
        qty: 2,
        item_id: null,
        item_name: "linen",
        unit: "each",
        on_hand: null,
      },
    ]);
  });

  it("prefers detail-level checklist and inventory effects when present", () => {
    const response: TaskDetailResponse = {
      task: apiTask({
        checklist: [{ id: "task-row", text: "Task row" }],
        inventory_consumption_json: { ignored: 1 },
      }),
      checklist: [{ id: "detail-row", label: "Detail row", done: true }],
      property: null,
      instructions: [],
      inventory_effects: [
        {
          item_ref: "soap",
          kind: "produce",
          qty: 1,
          item_id: "inv1",
          item_name: "Soap",
          unit: "box",
          on_hand: 4,
        },
      ],
    };

    const detail = normalizeTaskDetail(response);

    expect(detail.task.checklist).toEqual([
      { id: "detail-row", label: "Detail row", done: true, required: false },
    ]);
    expect(detail.inventory_effects).toEqual(response.inventory_effects);
  });

  it("updates both task and response checklist rows without mutating missing rows", () => {
    const response: TaskDetailResponse = {
      task: apiTask({ checklist: [{ id: "ci1", text: "Task row", checked: false }] }),
      checklist: [{ id: "ci1", text: "Detail row", checked: false }],
    };

    const updated = updateChecklistItem(response, "ci1", { checked: true });
    const unchanged = updateChecklistItem(undefined, "ci1", { checked: true });

    expect(unchanged).toBeUndefined();
    expect(normalizeTaskDetail(updated!).task.checklist).toEqual([
      { id: "ci1", label: "Detail row", done: true, required: false },
    ]);
    expect(response.checklist?.[0]?.checked).toBe(false);
  });
});
