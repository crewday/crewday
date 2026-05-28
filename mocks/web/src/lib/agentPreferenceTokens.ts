const APPROX_CHARS_PER_TOKEN = 3.8;

export function estimateAgentPreferenceTokens(text: string): number {
  if (text.length === 0) return 0;
  return Math.max(1, Math.ceil(text.length / APPROX_CHARS_PER_TOKEN));
}
