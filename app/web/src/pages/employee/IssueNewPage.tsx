import { useMemo, useRef } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Camera } from "lucide-react";
import { Loading } from "@/components/common";
import PageHeader from "@/components/PageHeader";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import SearchableSelect from "@/components/SearchableSelect";
import { useNavHistory } from "@/context/NavHistoryContext";
import { propertySelectOption } from "@/lib/propertySelectOptions";
import { cap } from "@/lib/strings";
import { workspaceRouteForPathname } from "@/lib/workspaceRoutes";
import { usePatchReducer } from "@/lib/usePatchReducer";
import type { Issue, Property } from "@/types/api";

type Category = "damage" | "broken" | "supplies" | "safety" | "other";
type Severity = "low" | "normal" | "high" | "urgent";

const CATEGORIES: Category[] = ["damage", "broken", "supplies", "safety", "other"];
const SEVERITIES: [Severity, string][] = [
  ["low", "Low"],
  ["normal", "Normal"],
  ["high", "High, unsafe or guest-facing"],
  ["urgent", "Urgent, needs action today"],
];

interface NewIssueBody {
  title: string;
  severity: Severity;
  category: Category;
  property_id: string;
  area: string;
  body: string;
}

interface IssueFormState {
  title: string;
  propertyId: string;
  area: string;
  category: Category;
  severity: Severity;
  body: string;
  submitError: string | null;
}

export default function IssueNewPage() {
  // code-health: ignore[nloc] Issue form is a single declarative workflow after shared query/offline helpers.
  const nav = useNavigate();
  const { pathname } = useLocation();
  const { canGoBack } = useNavHistory();
  const qc = useQueryClient();
  const photoInputRef = useRef<HTMLInputElement>(null);
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  const [form, setForm] = usePatchReducer<IssueFormState>({
    title: "",
    propertyId: "",
    area: "",
    category: "broken",
    severity: "normal",
    body: "",
    submitError: null,
  });
  const { title, propertyId, area, category, severity, body, submitError } = form;

  const create = useMutation({
    mutationFn: (payload: NewIssueBody) =>
      fetchJson<Issue>("/api/v1/issues", { method: "POST", body: payload }),
    onMutate: () => {
      setForm({ submitError: null });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.issues() });
      qc.invalidateQueries({ queryKey: qk.dashboard() });
      if (canGoBack) {
        nav(-1);
      } else {
        nav(workspaceRouteForPathname(pathname, "/me"));
      }
    },
    onError: (err: Error) => {
      setForm({ submitError: err.message });
    },
  });

  const header = (
    <PageHeader
      title="Report an issue"
      sub="Tell the manager something is broken, missing, or unsafe. The more specific the better."
    />
  );
  const properties = propsQ.data ?? [];
  const propertyOptions = useMemo(() => properties.map(propertySelectOption), [properties]);

  if (propsQ.isPending) return <>{header}<section className="phone__section"><Loading /></section></>;
  if (propsQ.isError || !propsQ.data) {
    return <>{header}<section className="phone__section"><p className="muted">Failed to load.</p></section></>;
  }

  const activePropertyId = propertyId || properties[0]?.id || "";

  return (
    <>
      {header}
      <section className="phone__section">
        <p className="muted">
          You can also report this in <Link to={workspaceRouteForPathname(pathname, "/chat")} className="issue-new__chat-link">Chat</Link>, it's usually faster.
        </p>
        {submitError && <p className="muted" role="alert">{submitError}</p>}

      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate({
            title,
            severity,
            category,
            property_id: activePropertyId,
            area,
            body,
          });
        }}
      >
        <label className="field">
          <span>Short title</span>
          <input
            name="title"
            placeholder="e.g. Bathroom tap dripping"
            required
            value={title}
            onChange={(e) => setForm({ title: e.target.value })}
           aria-label="field Short title title e.g. Bathroom tap dripping"/>
        </label>

        <SearchableSelect
          label="Property"
          name="property_id"
          value={activePropertyId}
          options={propertyOptions}
          onChange={(value) => setForm({ propertyId: value })}
          required
        />

        <label className="field">
          <span>Area</span>
          <input
            name="area"
            placeholder="e.g. Master bathroom"
            value={area}
            onChange={(e) => setForm({ area: e.target.value })}
           aria-label="field Area area e.g. Master bathroom"/>
        </label>

        <label className="field">
          <span>Category</span>
          <div className="chip-group">
            {CATEGORIES.map((c) => (
              <label key={c} className="chip-radio">
                <input
                  type="radio"
                  name="category"
                  value={c}
                  checked={category === c}
                  onChange={() => setForm({ category: c })}
                 aria-label="chip-radio radio category"/>
                <span>{cap(c)}</span>
              </label>
            ))}
          </div>
        </label>

        <label className="field">
          <span>Severity</span>
          <div className="chip-group">
            {SEVERITIES.map(([s, label]) => (
              <label key={s} className="chip-radio">
                <input
                  type="radio"
                  name="severity"
                  value={s}
                  checked={severity === s}
                  onChange={() => setForm({ severity: s })}
                 aria-label="chip-radio radio severity"/>
                <span>{label}</span>
              </label>
            ))}
          </div>
        </label>

        <div className="field">
          <span>What happened?</span>
          <AutoGrowTextarea
            name="body"
            aria-label="What happened?"
            placeholder="What you saw, what you tried, anything the manager should know."
            value={body}
            onChange={(e) => setForm({ body: e.target.value })}
          />
        </div>

        <div className="form__row">
          {/* Not migrated yet: issue creation still posts JSON and does not attach the selected file. */}
          <input
            ref={photoInputRef}
            className="sr-only"
            type="file"
            accept="image/*"
            capture="environment"
            aria-label="Photo file"
            tabIndex={-1}
          />
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => photoInputRef.current?.click()}
          >
            <Camera size={16} strokeWidth={1.8} aria-hidden="true" /> Attach photo
          </button>
          <button type="submit" className="btn btn--moss" disabled={create.isPending || !activePropertyId}>Send to manager</button>
        </div>
      </form>
      </section>
    </>
  );
}
