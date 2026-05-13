import type { LlmThinkingLevel, LlmThinkingStrategy } from "@/types";

export const THINKING_LEVEL_OPTIONS = [
  "disabled",
  "low",
  "medium",
  "high",
] as const satisfies readonly LlmThinkingLevel[];

export const THINKING_STRATEGY_OPTIONS = [
  "none",
  "gemma_system_token",
  "glm_extra_body",
  "openrouter_extra_body",
] as const satisfies readonly LlmThinkingStrategy[];

const THINKING_STRATEGY_LABELS: Record<LlmThinkingStrategy, string> = {
  none: "None / provider default",
  gemma_system_token: "Gemma system token",
  glm_extra_body: "GLM extra body",
  openrouter_extra_body: "OpenRouter reasoning body",
};

export function thinkingLevelLabel(level: LlmThinkingLevel): string {
  return level;
}

export function thinkingStrategyLabel(strategy: LlmThinkingStrategy): string {
  return THINKING_STRATEGY_LABELS[strategy];
}

export function isThinkingLevel(value: string): value is LlmThinkingLevel {
  return (THINKING_LEVEL_OPTIONS as readonly string[]).includes(value);
}

export function isThinkingStrategy(value: string): value is LlmThinkingStrategy {
  return (THINKING_STRATEGY_OPTIONS as readonly string[]).includes(value);
}
