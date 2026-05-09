import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import TokenCreateForm, {
  type TokenCreatePayload,
  type TokenScopeOption,
} from "@/components/TokenCreateForm";
import type { ApiTokenCreated } from "@/types/api";
import { WORKSPACE_SCOPES } from "./lib/tokenStatus";

interface MintTokenModalProps {
  onCreated: (created: ApiTokenCreated) => void;
  onCancel: () => void;
}

export default function MintTokenModal({ onCreated, onCancel }: MintTokenModalProps) {
  const qc = useQueryClient();

  const createM = useMutation({
    mutationFn: (body: TokenCreatePayload) =>
      fetchJson<ApiTokenCreated>("/api/v1/auth/tokens", { method: "POST", body }),
    onSuccess: (created) => {
      onCreated(created);
      qc.invalidateQueries({ queryKey: qk.apiTokens() });
    },
  });

  function submitCreate(payload: TokenCreatePayload) {
    createM.mutate(payload);
  }

  const scopeOptions: TokenScopeOption[] = WORKSPACE_SCOPES.map((key) => ({ key }));

  return (
    <section className="panel">
      <header className="panel__head">
        <h2>New workspace token</h2>
      </header>

      <TokenCreateForm
        labelId="tok-label"
        initialLabel="my-script"
        labelPlaceholder="my-script"
        labelMaxLength={160}
        scopes={scopeOptions}
        initialScopes={["tasks:read"]}
        allowDelegated
        isPending={createM.isPending}
        error={createM.isError ? (createM.error as Error) : null}
        actionHint="The plaintext secret is shown exactly once on the next screen. We store only an argon2id hash; if you lose it, rotate."
        onSubmit={submitCreate}
        onCancel={onCancel}
      />
    </section>
  );
}
