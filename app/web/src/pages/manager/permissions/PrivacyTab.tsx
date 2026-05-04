import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Checkbox, Loading } from "@/components/common";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type {
  UpstreamPiiConsentToken,
  WorkspaceUpstreamPiiConsent,
} from "@/types/api";

interface ConsentChoice {
  token: UpstreamPiiConsentToken;
  label: string;
  hint: string;
}

const CONSENT_CHOICES: ConsentChoice[] = [
  {
    token: "legal_name",
    label: "Legal names",
    hint: "Allow legal_name fields through the upstream LLM redaction seam.",
  },
  {
    token: "email",
    label: "Email addresses",
    hint: "Allow email fields through the upstream LLM redaction seam.",
  },
  {
    token: "phone",
    label: "Phone numbers",
    hint: "Allow phone fields through the upstream LLM redaction seam.",
  },
  {
    token: "address",
    label: "Addresses",
    hint: "Allow address fields through the upstream LLM redaction seam.",
  },
];

const ENDPOINT = "/api/v1/agent_preferences/workspace/upstream_pii_consent";

function nextConsent(
  current: UpstreamPiiConsentToken[],
  token: UpstreamPiiConsentToken,
): UpstreamPiiConsentToken[] {
  const selected = new Set(current);
  if (selected.has(token)) selected.delete(token);
  else selected.add(token);
  return CONSENT_CHOICES
    .map((choice) => choice.token)
    .filter((candidate) => selected.has(candidate));
}

export default function PrivacyTab() {
  const qc = useQueryClient();
  const key = qk.agentUpstreamPiiConsent();
  const consent = useQuery({
    queryKey: key,
    queryFn: () => fetchJson<WorkspaceUpstreamPiiConsent>(ENDPOINT),
  });

  const update = useMutation({
    mutationFn: (tokens: UpstreamPiiConsentToken[]) =>
      fetchJson<WorkspaceUpstreamPiiConsent>(ENDPOINT, {
        method: "PUT",
        body: { upstream_pii_consent: tokens },
      }),
    onMutate: async (tokens) => {
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<WorkspaceUpstreamPiiConsent>(key);
      qc.setQueryData(key, {
        upstream_pii_consent: tokens,
        available_tokens: prev?.available_tokens ?? CONSENT_CHOICES.map((choice) => choice.token),
      });
      return { prev };
    },
    onError: (_error, _tokens, ctx) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev);
    },
    onSuccess: (next) => {
      qc.setQueryData(key, next);
    },
  });

  if (consent.isPending) return <Loading />;
  if (!consent.data) return <div>Failed to load.</div>;

  const selected = consent.data.upstream_pii_consent;

  return (
    <section className="panel permissions__privacy">
      <header className="panel__head">
        <div className="panel__head-stack">
          <h2>Privacy</h2>
          <p className="panel__sub">
            Owner-managed consent for structured PII fields sent to upstream
            LLM providers.
          </p>
        </div>
      </header>

      {selected.length === 0 ? (
        <p className="agent-prefs__banner" role="status">
          No upstream PII consent selected. Legal names, email addresses,
          phone numbers, and addresses are redacted before model calls.
        </p>
      ) : null}

      <fieldset className="permissions__privacy-choices" aria-label="Upstream PII consent">
        {CONSENT_CHOICES.map((choice) => (
          <Checkbox
            key={choice.token}
            block
            tone="moss"
            checked={selected.includes(choice.token)}
            disabled={update.isPending}
            onChange={() => update.mutate(nextConsent(selected, choice.token))}
            label={<strong>{choice.label}</strong>}
            hint={choice.hint}
          />
        ))}
      </fieldset>
      {update.isError ? (
        <p className="agent-prefs__error">Could not update upstream PII consent.</p>
      ) : null}
    </section>
  );
}
