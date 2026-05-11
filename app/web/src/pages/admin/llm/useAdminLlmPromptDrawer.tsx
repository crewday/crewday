import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useCloseOnEscape } from "@/lib/useCloseOnEscape";
import type { LlmPromptTemplate } from "@/types";
import PromptLibraryDrawer from "./PromptLibraryDrawer";

export function useAdminLlmPromptDrawer() {
  const promptsQ = useQuery({
    queryKey: qk.adminLlmPrompts(),
    queryFn: () => fetchJson<LlmPromptTemplate[]>("/admin/api/v1/llm/prompts"),
  });
  const [promptsOpen, setPromptsOpen] = useState(false);
  useCloseOnEscape(() => setPromptsOpen(false), promptsOpen);

  return {
    promptsQ,
    promptOverflow: {
      label: "Prompts",
      onSelect: () => setPromptsOpen(true),
    },
    promptDrawer:
      promptsOpen && promptsQ.data ? (
        <PromptLibraryDrawer
          prompts={promptsQ.data}
          onClose={() => setPromptsOpen(false)}
        />
      ) : null,
  };
}
