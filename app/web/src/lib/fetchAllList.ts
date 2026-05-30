import { fetchJson } from "@/lib/api";
import type { ListEnvelope } from "@/lib/listResponse";

export async function fetchAllList<T>(path: string): Promise<T[]> {
  const rows: T[] = [];
  let cursor: string | null = null;

  do {
    const params = new URLSearchParams({ limit: "500" });
    if (cursor !== null) params.set("cursor", cursor);
    // react-doctor-disable-next-line react-doctor/async-await-in-loop -- Each page cursor comes from the previous response.
    const page = await fetchJson<ListEnvelope<T>>(path + "?" + params.toString());
    rows.push(...page.data);
    cursor = page.has_more ? page.next_cursor : null;
  } while (cursor !== null);

  return rows;
}
