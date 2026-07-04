import { describe, expect, it } from "vitest";

import { clearMapValue, setMapValue } from "@/lib/mapState";

describe("setMapValue", () => {
  it("returns a new map with the value set, leaving the original untouched", () => {
    const original: ReadonlyMap<string, number> = new Map([["a", 1]]);
    const next = setMapValue(original, "b", 2);
    expect(next).not.toBe(original);
    expect([...next]).toEqual([["a", 1], ["b", 2]]);
    expect([...original]).toEqual([["a", 1]]);
  });

  it("overwrites an existing key", () => {
    const next = setMapValue(new Map([["a", 1]]), "a", 9);
    expect(next.get("a")).toBe(9);
  });
});

describe("clearMapValue", () => {
  it("returns the same reference when the key is absent", () => {
    const original: ReadonlyMap<string, number> = new Map([["a", 1]]);
    expect(clearMapValue(original, "missing")).toBe(original);
  });

  it("returns a new map without the key when present", () => {
    const original: ReadonlyMap<string, number> = new Map([["a", 1], ["b", 2]]);
    const next = clearMapValue(original, "a");
    expect(next).not.toBe(original);
    expect([...next]).toEqual([["b", 2]]);
    expect([...original]).toEqual([["a", 1], ["b", 2]]);
  });
});
