import { Chip } from "@/components/common";
import type { LlmPromptTemplate } from "@/types";

interface PromptLibraryDrawerProps {
  prompts: LlmPromptTemplate[];
  onClose: () => void;
}

export default function PromptLibraryDrawer({
  prompts,
  onClose,
}: PromptLibraryDrawerProps) {
  return (
    <div
      className="llm-prompt-drawer-backdrop"
      onClick={onClose}
      role="presentation"
    >
      <aside className="llm-prompt-drawer" onClick={(e) => e.stopPropagation()}>
        <header className="llm-prompt-drawer__head">
          <h2>Prompt library</h2>
          <button className="btn btn--ghost" onClick={onClose}>
            Close
          </button>
        </header>
        <p className="llm-prompt-drawer__hint muted">
          Hash-self-seeding: code defaults seed the row; unmodified prompts
          auto-upgrade when code changes; customisations are preserved.
        </p>
        <ul className="llm-prompt-list">
          {prompts.map((p) => (
            <li key={p.id} className="llm-prompt-list__item">
              <div className="llm-prompt-list__head">
                <code className="inline-code">{p.capability}</code>
                <span className="llm-prompt-list__name">{p.name}</span>
                <span className="llm-prompt-list__ver mono muted">v{p.version}</span>
                {p.is_customised ? (
                  <Chip tone="sand" size="sm">
                    customised
                  </Chip>
                ) : (
                  <Chip tone="ghost" size="sm">
                    default
                  </Chip>
                )}
              </div>
              <p className="llm-prompt-list__preview">{p.preview}</p>
              <footer className="llm-prompt-list__foot muted">
                <span>
                  {p.revisions_count} revision
                  {p.revisions_count === 1 ? "" : "s"}
                </span>
                <span>hash {p.default_hash}</span>
              </footer>
            </li>
          ))}
        </ul>
      </aside>
    </div>
  );
}
