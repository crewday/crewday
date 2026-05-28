import { describe, expect, it } from "vitest";
import { estimateAgentPreferenceTokens } from "@/lib/agentPreferenceTokens";

describe("agent preference token estimates", () => {
  it("returns zero for empty text", () => {
    expect(estimateAgentPreferenceTokens("")).toBe(0);
  });

  it("returns a positive deterministic estimate for non-empty text", () => {
    const text = "Always show EUR amounts in French accounting format.";

    expect(estimateAgentPreferenceTokens(text)).toBe(14);
    expect(estimateAgentPreferenceTokens(text)).toBe(14);
  });
});
