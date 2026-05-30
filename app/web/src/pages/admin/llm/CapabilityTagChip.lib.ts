const CAPABILITY_TAG_LABEL: Record<string, string> = {
  chat: "chat",
  vision: "vision",
  audio_input: "audio",
  reasoning: "reasoning",
  function_calling: "tools",
  json_mode: "json",
  streaming: "stream",
  embeddings: "embed",
};

export function capabilityTagLabel(tag: string): string {
  return CAPABILITY_TAG_LABEL[tag] ?? tag;
}
