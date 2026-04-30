import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type { AgentDoc, AgentDocSummary } from "@/types/api";

interface OpenApiSpec {
  paths?: Record<string, Record<string, unknown>>;
}

export default function AdminAgentDocsPage() {
  const [activeSlug, setActiveSlug] = useState<string | null>(null);
  const [openApiFilter, setOpenApiFilter] = useState("");

  const listQ = useQuery({
    queryKey: qk.adminAgentDocs(),
    queryFn: () => fetchJson<AgentDocSummary[]>("/admin/api/v1/agent_docs"),
  });

  const docQ = useQuery({
    queryKey: qk.adminAgentDoc(activeSlug ?? ""),
    queryFn: () => fetchJson<AgentDoc>(`/admin/api/v1/agent_docs/${activeSlug}`),
    enabled: activeSlug != null,
  });

  const openApiQ = useQuery({
    queryKey: ["admin", "openapi"],
    queryFn: () => fetchJson<OpenApiSpec>("/api/openapi.json"),
  });

  const sub =
    "System-side virtual files the chat agents read on demand (\u00a711 \u201cAgent knowledge tools\u201d).";

  if (listQ.isPending) {
    return <DeskPage title="Agent docs" sub={sub}><Loading /></DeskPage>;
  }
  if (!listQ.data) {
    return <DeskPage title="Agent docs" sub={sub}>Failed to load.</DeskPage>;
  }

  const openApiNeedle = openApiFilter.trim().toLowerCase();
  const openApiRows = Object.entries(openApiQ.data?.paths ?? {}).filter(([path]) =>
    path.toLowerCase().includes(openApiNeedle),
  );

  return (
    <DeskPage title="Agent docs" sub={sub}>
      <div className="agent-docs">
        <section className="panel agent-docs__list">
          <table className="table">
            <thead>
              <tr>
                <th>Slug</th>
                <th>Title</th>
                <th>Roles</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {listQ.data.map((d) => (
                <tr
                  key={d.slug}
                  className={d.slug === activeSlug ? "row--active" : ""}
                  onClick={() => setActiveSlug(d.slug)}
                >
                  <td className="mono">{d.slug}</td>
                  <td>
                    <strong>{d.title}</strong>
                    <span className="table__sub">{d.summary}</span>
                  </td>
                  <td>
                    {d.roles.map((r) => (
                      <Chip key={r} tone="ghost" size="sm">{r}</Chip>
                    ))}
                  </td>
                  <td className="muted">
                    {new Date(d.updated_at).toLocaleDateString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        {activeSlug && (
          <section className="panel agent-docs__detail">
            {docQ.isPending && <Loading />}
            {docQ.data && (
              <>
                <header className="agent-docs__header">
                  <h3>{docQ.data.title}</h3>
                  <span className="muted">v{docQ.data.version}</span>
                  {docQ.data.is_customised && (
                    <Chip tone="sand" size="sm">customised</Chip>
                  )}
                </header>
                <p className="muted agent-docs__summary">{docQ.data.summary}</p>
                <div className="agent-docs__meta">
                  <span className="muted">capabilities:</span>{" "}
                  {docQ.data.capabilities.map((c) => (
                    <Chip key={c} tone="ghost" size="sm">{c}</Chip>
                  ))}
                </div>
                <pre className="agent-docs__body">{docQ.data.body_md}</pre>
              </>
            )}
          </section>
        )}

        <section className="panel agent-docs__openapi">
          <header className="agent-docs__header">
            <h3>OpenAPI</h3>
            {openApiQ.isPending && <span className="muted">loading</span>}
          </header>
          <input
            className="input"
            type="search"
            value={openApiFilter}
            onChange={(event) => setOpenApiFilter(event.target.value)}
            placeholder="Search endpoints"
            aria-label="Search OpenAPI endpoints"
          />
          <table className="table">
            <thead>
              <tr>
                <th>Path</th>
                <th>Methods</th>
              </tr>
            </thead>
            <tbody>
              {openApiRows.map(([path, methods]) => (
                <tr key={path}>
                  <td className="mono">{path}</td>
                  <td className="muted">
                    {Object.keys(methods)
                      .filter((method) => method !== "parameters")
                      .join(", ")
                      .toUpperCase()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </DeskPage>
  );
}
