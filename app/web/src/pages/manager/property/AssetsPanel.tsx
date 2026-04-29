import { Chip } from "@/components/common";
import type { PropertyDetail } from "./types";

export default function AssetsPanel({ detail }: { detail: PropertyDetail }) {
  const { assets } = detail;

  return (
    <div className="panel">
      <header className="panel__head">
        <h2>Assets</h2>
        <span className="muted mono">{assets.length} tracked</span>
      </header>
      {assets.length === 0 ? (
        <p className="muted">No assets tracked for this property.</p>
      ) : (
        <table className="table">
          <thead>
            <tr><th>Asset</th><th>Area</th><th>Condition</th><th>Status</th></tr>
          </thead>
          <tbody>
            {assets.map((a) => (
              <tr key={a.id}>
                <td><strong>{a.name}</strong>{a.make && <span className="table__sub"> {a.make} {a.model}</span>}</td>
                <td>{a.area ?? "—"}</td>
                <td><Chip tone={a.condition === "fair" ? "sand" : (a.condition === "poor" || a.condition === "needs_replacement") ? "rust" : "moss"} size="sm">{a.condition}</Chip></td>
                <td><Chip tone={a.status === "active" ? "moss" : a.status === "in_repair" ? "sand" : "rust"} size="sm">{a.status}</Chip></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
