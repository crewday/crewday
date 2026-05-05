import { describe, expect, it } from "vitest";
import { normalizeTask, normalizeTodayPayload } from "./todayMappers";
import type { ApiTask, TaskListResponse } from "./todayMappers";

function apiTask(overrides: Partial<ApiTask> = {}): ApiTask {
  return {
    id: "t1",
    title: "Restock pantry",
    priority: "normal",
    state: "pending",
    photo_evidence: "disabled",
    ...overrides,
  };
}

describe("today mappers", () => {
  it("normalizes missing optional task fields with stable fallbacks", () => {
    const normalized = normalizeTask(
      apiTask({
        property_id: null,
        area_id: null,
        assigned_user_id: null,
        checklist: [
          { text: "Check bins", checked: true, required: true },
          { text: "" },
          {},
        ],
      }),
      "2026-04-28T12:00:00.000Z",
    );

    expect(normalized).toMatchObject({
      id: "t1",
      property_id: "",
      area: "",
      assigned_user_id: "",
      scheduled_start: "2026-04-28T12:00:00.000Z",
      estimated_minutes: 30,
      status: "pending",
      evidence_policy: "forbid",
      is_personal: false,
    });
    expect(normalized.checklist).toEqual([
      {
        label: "Check bins",
        done: true,
        guest_visible: undefined,
        key: undefined,
        required: true,
      },
    ]);
  });

  it("groups malformed and terminal rows without surfacing them as actionable", () => {
    const page: TaskListResponse = {
      data: [
        apiTask({ id: "now", scheduled_for_utc: "2026-04-28T09:00:00Z" }),
        apiTask({ id: "future", scheduled_start: "2026-04-28T14:00:00Z" }),
        apiTask({ id: "done", state: "completed", scheduled_for_local: "2026-04-28T08:00:00" }),
        apiTask({ id: "skipped", state: "skipped", scheduled_for_utc: "2026-04-28T07:00:00Z" }),
      ],
      next_cursor: null,
      has_more: false,
    };

    const grouped = normalizeTodayPayload(page, "2026-04-28T10:00:00Z");

    expect(grouped.now_task?.id).toBe("now");
    expect(grouped.upcoming.map((task) => task.id)).toEqual(["future"]);
    expect(grouped.completed.map((task) => task.id)).toEqual(["done"]);
    expect(grouped).not.toContainEqual(expect.objectContaining({ id: "skipped" }));
  });
});
