import { fetchJson } from "@/lib/api";
import type { ListEnvelope } from "@/lib/listResponse";
import type { AssetDocument } from "@/types/api";

function unwrapDocuments(payload: AssetDocument[] | ListEnvelope<AssetDocument>): AssetDocument[] {
  return Array.isArray(payload) ? payload : payload.data;
}

export async function fetchDocumentList(path: string): Promise<AssetDocument[]> {
  return unwrapDocuments(await fetchJson<AssetDocument[] | ListEnvelope<AssetDocument>>(path));
}
