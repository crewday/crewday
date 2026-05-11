import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import TokenCreateForm, {
  type TokenCreatePayload,
  type TokenScopeOption,
} from "@/components/TokenCreateForm";
import FormModal from "@/components/FormModal";
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
    <FormModal
      open
      title="New workspace token"
      eyebrow="API token"
      subtitle="Create a scoped or delegated bearer token for this workspace."
      contentElement="section"
      onClose={onCancel}
      formClassName="tokens-create-modal"
      bodyClassName="tokens-create-modal__body"
    >
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
    </FormModal>
  );
}
