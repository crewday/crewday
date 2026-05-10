import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { PackageSearch, SearchX } from "lucide-react";
import { ApiError, fetchJson, resolveApiPath } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import FileDropZone from "@/components/FileDropZone";
import { Checkbox, Chip, EmptyState, FilterChipGroup, Loading } from "@/components/common";
import { AssetIcon } from "@/components/AssetIcon";
import { ASSET_CONDITION_TONE, ASSET_STATUS_TONE } from "@/lib/tones";
import { workspaceRouteForPathname } from "@/lib/workspaceRoutes";
import { type ListEnvelope, unwrapList as unwrapEnvelope } from "@/lib/listResponse";
import type {
  Asset,
  AssetCondition,
  AssetDocument,
  AssetStatus,
  AssetType,
  DocumentKind,
  Property,
} from "@/types/api";

function unwrapList<T>(payload: T[] | ListEnvelope<T>): T[] {
  return Array.isArray(payload) ? payload : payload.data;
}

async function fetchList<T>(path: string): Promise<T[]> {
  return unwrapList(await fetchJson<T[] | ListEnvelope<T>>(path));
}

interface AreaOption {
  id: string;
  name: string;
}

interface AssetCreateBody {
  name: string;
  property_id: string;
  asset_type_id?: string;
  area_id?: string;
  make?: string;
  model?: string;
  serial_number?: string;
  condition: AssetCondition;
  status: AssetStatus;
  installed_on?: string;
  purchased_on?: string;
  purchase_price_cents?: number;
  purchase_currency?: string;
  purchase_vendor?: string;
  warranty_expires_on?: string;
  expected_lifespan_years?: number;
  guest_visible: boolean;
  guest_instructions_md?: string;
  notes_md?: string;
}

interface QueuedAssetDocument {
  localId: string;
  file: File;
  kind: DocumentKind;
  title: string;
}

class AssetDocumentUploadError extends Error {
  readonly asset: Asset;
  readonly failedDocuments: QueuedAssetDocument[];

  constructor(asset: Asset, failedDocuments: QueuedAssetDocument[]) {
    super(
      `Asset created, but ${failedDocuments.length} document upload${
        failedDocuments.length === 1 ? "" : "s"
      } failed.`,
    );
    this.name = "AssetDocumentUploadError";
    this.asset = asset;
    this.failedDocuments = failedDocuments;
  }
}

const ASSET_CONDITIONS: { value: AssetCondition; label: string }[] = [
  { value: "new", label: "New" },
  { value: "good", label: "Good" },
  { value: "fair", label: "Fair" },
  { value: "poor", label: "Poor" },
  { value: "needs_replacement", label: "Needs replacement" },
];

const ASSET_STATUSES: { value: AssetStatus; label: string }[] = [
  { value: "active", label: "Active" },
  { value: "in_repair", label: "In repair" },
  { value: "decommissioned", label: "Decommissioned" },
  { value: "disposed", label: "Disposed" },
];

const DOCUMENT_KINDS: { value: DocumentKind; label: string }[] = [
  { value: "manual", label: "Manual" },
  { value: "warranty", label: "Warranty" },
  { value: "invoice", label: "Invoice" },
  { value: "receipt", label: "Receipt" },
  { value: "photo", label: "Photo" },
  { value: "certificate", label: "Certificate" },
  { value: "contract", label: "Contract" },
  { value: "permit", label: "Permit" },
  { value: "insurance", label: "Insurance" },
  { value: "other", label: "Other" },
];

function setSearchParam(
  current: URLSearchParams,
  key: string,
  value: string,
): URLSearchParams {
  const next = new URLSearchParams(current);
  if (value) {
    next.set(key, value);
  } else {
    next.delete(key);
  }
  return next;
}

function QrSheetButton({
  category,
  propertyId,
}: {
  category: string;
  propertyId: string;
}) {
  const params = new URLSearchParams();
  if (category) params.set("category", category);
  if (propertyId) params.set("property_id", propertyId);
  const suffix = params.toString() ? "?" + params.toString() : "";
  return (
    <button
      className="btn"
      onClick={() =>
        window.open(
          resolveApiPath("/api/v1/assets/qr-sheet" + suffix),
          "_blank",
          "noopener,noreferrer",
        )
      }
    >
      Print QR labels
    </button>
  );
}

function NewAssetButton({
  assetTypes,
  properties,
  activePropertyId,
}: {
  assetTypes: AssetType[] | undefined;
  properties: Property[] | undefined;
  activePropertyId: string;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [name, setName] = useState("");
  const [propertyId, setPropertyId] = useState("");
  const [assetTypeId, setAssetTypeId] = useState("");
  const [areaId, setAreaId] = useState("");
  const [make, setMake] = useState("");
  const [model, setModel] = useState("");
  const [serialNumber, setSerialNumber] = useState("");
  const [condition, setCondition] = useState<AssetCondition>("good");
  const [status, setStatus] = useState<AssetStatus>("active");
  const [installedOn, setInstalledOn] = useState("");
  const [purchasedOn, setPurchasedOn] = useState("");
  const [purchasePrice, setPurchasePrice] = useState("");
  const [purchaseCurrency, setPurchaseCurrency] = useState("");
  const [purchaseVendor, setPurchaseVendor] = useState("");
  const [warrantyExpiresOn, setWarrantyExpiresOn] = useState("");
  const [expectedLifespanYears, setExpectedLifespanYears] = useState("");
  const [guestVisible, setGuestVisible] = useState(false);
  const [guestInstructions, setGuestInstructions] = useState("");
  const [notes, setNotes] = useState("");
  const [queuedDocuments, setQueuedDocuments] = useState<QueuedAssetDocument[]>([]);
  const [createdAssetAfterUploadFailure, setCreatedAssetAfterUploadFailure] =
    useState<Asset | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const areasQ = useQuery({
    queryKey: qk.propertyAreas(propertyId),
    queryFn: () =>
      fetchJson<ListEnvelope<AreaOption>>(
        "/api/v1/properties/" + propertyId + "/areas",
      ).then(unwrapEnvelope),
    enabled: dialogOpen && Boolean(propertyId),
  });

  const create = useMutation({
    mutationFn: async (body: AssetCreateBody) => {
      const asset =
        createdAssetAfterUploadFailure ??
        (await fetchJson<Asset>("/api/v1/assets", { method: "POST", body }));
      if (queuedDocuments.length > 0) {
        await uploadQueuedAssetDocuments(asset, queuedDocuments);
      }
      return asset;
    },
    onSuccess: async (asset) => {
      setFormError(null);
      setCreatedAssetAfterUploadFailure(null);
      await queryClient.invalidateQueries({ queryKey: qk.assets() });
      await queryClient.invalidateQueries({ queryKey: qk.documents() });
      await queryClient.invalidateQueries({ queryKey: qk.asset(asset.id) });
      dialogRef.current?.close();
    },
    onError: (error) => {
      if (error instanceof AssetDocumentUploadError) {
        setCreatedAssetAfterUploadFailure(error.asset);
        setQueuedDocuments(error.failedDocuments);
        setFormError(assetDocumentUploadErrorMessage(error));
        void queryClient.invalidateQueries({ queryKey: qk.assets() });
        void queryClient.invalidateQueries({ queryKey: qk.documents() });
        void queryClient.invalidateQueries({ queryKey: qk.asset(error.asset.id) });
        return;
      }
      setFormError(assetCreateErrorMessage(error));
    },
  });

  function reset(): void {
    if (create.isPending) return;
    setDialogOpen(false);
    setName("");
    setPropertyId("");
    setAssetTypeId("");
    setAreaId("");
    setMake("");
    setModel("");
    setSerialNumber("");
    setCondition("good");
    setStatus("active");
    setInstalledOn("");
    setPurchasedOn("");
    setPurchasePrice("");
    setPurchaseCurrency("");
    setPurchaseVendor("");
    setWarrantyExpiresOn("");
    setExpectedLifespanYears("");
    setGuestVisible(false);
    setGuestInstructions("");
    setNotes("");
    setQueuedDocuments([]);
    setCreatedAssetAfterUploadFailure(null);
    setFormError(null);
    create.reset();
  }

  function openDialog(): void {
    const fallbackPropertyId = properties?.[0]?.id ?? "";
    const activePropertyIsAvailable = properties?.some(
      (property) => property.id === activePropertyId,
    );
    setPropertyId(
      activePropertyIsAvailable ? activePropertyId : fallbackPropertyId,
    );
    setDialogOpen(true);
    dialogRef.current?.showModal();
  }

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (create.isPending) return;
    const body = buildAssetCreateBody({
      name,
      propertyId,
      assetTypeId,
      areaId,
      make,
      model,
      serialNumber,
      condition,
      status,
      installedOn,
      purchasedOn,
      purchasePrice,
      purchaseCurrency,
      purchaseVendor,
      warrantyExpiresOn,
      expectedLifespanYears,
      guestVisible,
      guestInstructions,
      notes,
    });
    if (typeof body === "string") {
      setFormError(body);
      return;
    }
    setFormError(null);
    create.mutate(body);
  }

  function addDocuments(files: File[]): void {
    if (files.length === 0) return;
    setQueuedDocuments((current) => [
      ...current,
      ...files.map((file) => ({
        localId: `${file.name}-${file.size}-${file.lastModified}-${crypto.randomUUID()}`,
        file,
        kind: defaultDocumentKind(file),
        title: defaultDocumentTitle(file),
      })),
    ]);
    setFormError(null);
  }

  function updateDocument(
    localId: string,
    patch: Partial<Pick<QueuedAssetDocument, "kind" | "title">>,
  ): void {
    setQueuedDocuments((current) =>
      current.map((doc) => (doc.localId === localId ? { ...doc, ...patch } : doc)),
    );
    setFormError(null);
  }

  function removeDocument(localId: string): void {
    setQueuedDocuments((current) => current.filter((doc) => doc.localId !== localId));
    setFormError(null);
  }

  const ready = Boolean(assetTypes && properties);
  const hasProperties = Boolean(properties?.length);
  const disabledReason = !ready
    ? "Asset options are still loading."
    : !hasProperties
      ? "Add a property before creating an asset."
      : null;
  const formErrorId = formError ? "asset-create-error" : undefined;
  const submitLabel = createdAssetAfterUploadFailure
    ? create.isPending
      ? "Uploading..."
      : "Retry document uploads"
    : create.isPending
      ? queuedDocuments.length > 0
        ? "Creating and uploading..."
        : "Creating..."
      : "Create asset";
  const nameInvalid = formError === "Enter an asset name before creating the asset.";
  const propertyInvalid = formError === "Choose a property for this asset.";
  const currencyInvalid = formError === "Use a three-letter currency code, such as USD.";
  const priceInvalid =
    formError === "Purchase price must be zero or more with up to two decimal places.";
  const lifespanInvalid = formError === "Expected lifespan must be at least one year.";

  return (
    <>
      <button
        type="button"
        className="btn btn--moss"
        disabled={Boolean(disabledReason)}
        title={disabledReason ?? undefined}
        onClick={openDialog}
      >
        + New asset
      </button>

      <dialog
        className="modal modal--sheet asset-create-dialog"
        ref={dialogRef}
        aria-labelledby="new-asset-title"
        onCancel={(event) => {
          if (create.isPending) event.preventDefault();
        }}
        onClose={reset}
      >
        <form className="asset-create" onSubmit={submit} noValidate>
          <header className="asset-create__head">
            <div>
              <p className="asset-create__eyebrow">Asset record</p>
              <h3 id="new-asset-title" className="asset-create__title">New asset</h3>
              <p className="asset-create__sub">
                Track equipment identity, location, purchase details, and guest visibility.
              </p>
            </div>
            <button
              type="button"
              className="asset-create__close"
              disabled={create.isPending}
              onClick={() => dialogRef.current?.close()}
              aria-label="Close"
            >
              ×
            </button>
          </header>

          <div className="asset-create__body">
            <section className="asset-create__section" aria-labelledby="asset-create-basics">
              <h4 id="asset-create-basics" className="asset-create__section-title">
                Basics and location
              </h4>
              <label className="field asset-create__field">
                <span>Name</span>
                <input
                  autoFocus
                  required
                  aria-invalid={nameInvalid}
                  aria-describedby={nameInvalid ? formErrorId : undefined}
                  value={name}
                  onChange={(event) => {
                    setName(event.target.value);
                    setFormError(null);
                  }}
                  placeholder="e.g. Kitchen fridge"
                />
              </label>
              <div className="asset-create__grid">
                <label className="field asset-create__field">
                  <span>Property</span>
                  <select
                    required
                    aria-invalid={propertyInvalid}
                    aria-describedby={propertyInvalid ? formErrorId : undefined}
                    value={propertyId}
                    onChange={(event) => {
                      setPropertyId(event.target.value);
                      setAreaId("");
                      setFormError(null);
                    }}
                  >
                    <option value="">Choose property</option>
                    {properties?.map((property) => (
                      <option key={property.id} value={property.id}>{property.name}</option>
                    ))}
                  </select>
                </label>
                <label className="field asset-create__field">
                  <span>Area</span>
                  <select
                    value={areaId}
                    disabled={!propertyId || areasQ.isPending || areasQ.isError}
                    onChange={(event) => {
                      setAreaId(event.target.value);
                      setFormError(null);
                    }}
                  >
                    <option value="">No area</option>
                    {areasQ.data?.map((area) => (
                      <option key={area.id} value={area.id}>{area.name}</option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="asset-create__grid">
                <label className="field asset-create__field">
                  <span>Type</span>
                  <select
                    value={assetTypeId}
                    onChange={(event) => {
                      setAssetTypeId(event.target.value);
                      setFormError(null);
                    }}
                  >
                    <option value="">Uncategorized</option>
                    {assetTypes?.map((assetType) => (
                      <option key={assetType.id} value={assetType.id}>{assetType.name}</option>
                    ))}
                  </select>
                </label>
                <label className="field asset-create__field">
                  <span>Condition</span>
                  <select
                    value={condition}
                    onChange={(event) => setCondition(event.target.value as AssetCondition)}
                  >
                    {ASSET_CONDITIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                </label>
              </div>
            </section>

            <section className="asset-create__section" aria-labelledby="asset-create-identity">
              <h4 id="asset-create-identity" className="asset-create__section-title">
                Identity
              </h4>
              <div className="asset-create__grid">
                <label className="field asset-create__field">
                  <span>Make</span>
                  <input value={make} onChange={(event) => setMake(event.target.value)} />
                </label>
                <label className="field asset-create__field">
                  <span>Model</span>
                  <input value={model} onChange={(event) => setModel(event.target.value)} />
                </label>
              </div>
              <div className="asset-create__grid">
                <label className="field asset-create__field">
                  <span>Serial number</span>
                  <input
                    value={serialNumber}
                    onChange={(event) => setSerialNumber(event.target.value)}
                  />
                </label>
                <label className="field asset-create__field">
                  <span>Status</span>
                  <select
                    value={status}
                    onChange={(event) => setStatus(event.target.value as AssetStatus)}
                  >
                    {ASSET_STATUSES.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                </label>
              </div>
            </section>

            <section className="asset-create__section" aria-labelledby="asset-create-purchase">
              <h4 id="asset-create-purchase" className="asset-create__section-title">
                Purchase and warranty
              </h4>
              <div className="asset-create__grid">
                <label className="field asset-create__field">
                  <span>Installed on</span>
                  <input
                    type="date"
                    value={installedOn}
                    onChange={(event) => setInstalledOn(event.target.value)}
                  />
                </label>
                <label className="field asset-create__field">
                  <span>Purchased on</span>
                  <input
                    type="date"
                    value={purchasedOn}
                    onChange={(event) => setPurchasedOn(event.target.value)}
                  />
                </label>
              </div>
              <div className="asset-create__grid">
                <label className="field asset-create__field">
                  <span>Purchase price</span>
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    aria-invalid={priceInvalid}
                    aria-describedby={priceInvalid ? formErrorId : undefined}
                    value={purchasePrice}
                    onChange={(event) => setPurchasePrice(event.target.value)}
                    placeholder="0.00"
                  />
                </label>
                <label className="field asset-create__field">
                  <span>Currency</span>
                  <input
                    aria-invalid={currencyInvalid}
                    aria-describedby={currencyInvalid ? formErrorId : undefined}
                    value={purchaseCurrency}
                    onChange={(event) => setPurchaseCurrency(event.target.value)}
                    placeholder="USD"
                    maxLength={3}
                  />
                </label>
              </div>
              <div className="asset-create__grid">
                <label className="field asset-create__field">
                  <span>Vendor</span>
                  <input
                    value={purchaseVendor}
                    onChange={(event) => setPurchaseVendor(event.target.value)}
                  />
                </label>
                <label className="field asset-create__field">
                  <span>Warranty expires</span>
                  <input
                    type="date"
                    value={warrantyExpiresOn}
                    onChange={(event) => setWarrantyExpiresOn(event.target.value)}
                  />
                </label>
              </div>
              <label className="field asset-create__field asset-create__field--short">
                <span>Expected lifespan years</span>
                <input
                  type="number"
                  min="1"
                  step="1"
                  aria-invalid={lifespanInvalid}
                  aria-describedby={lifespanInvalid ? formErrorId : undefined}
                  value={expectedLifespanYears}
                  onChange={(event) => setExpectedLifespanYears(event.target.value)}
                />
              </label>
            </section>

            <section className="asset-create__section" aria-labelledby="asset-create-guest">
              <h4 id="asset-create-guest" className="asset-create__section-title">
                Guest visibility
              </h4>
              <Checkbox
                className="asset-create__checkbox"
                label="Visible to guests"
                checked={guestVisible}
                onChange={(event) => setGuestVisible(event.target.checked)}
              />
              <label className="field asset-create__field">
                <span>Guest instructions</span>
                <textarea
                  value={guestInstructions}
                  onChange={(event) => setGuestInstructions(event.target.value)}
                />
              </label>
            </section>

            <section className="asset-create__section" aria-labelledby="asset-create-notes">
              <h4 id="asset-create-notes" className="asset-create__section-title">
                Notes
              </h4>
              <label className="field asset-create__field">
                <span>Internal notes</span>
                <textarea value={notes} onChange={(event) => setNotes(event.target.value)} />
              </label>
            </section>

            <section className="asset-create__section" aria-labelledby="asset-create-documents">
              <h4 id="asset-create-documents" className="asset-create__section-title">
                Documents
              </h4>
              <FileDropZone
                className="asset-create__upload"
                title="Upload or drop invoices, manuals, warranties, receipts, certificates, photos, or permits"
                description="Select multiple files or drop them here."
                multiple
                disabled={create.isPending}
                onFiles={addDocuments}
              />
              {createdAssetAfterUploadFailure ? (
                <p className="form-notice form-notice--error" role="status">
                  The asset was created. Retry the failed document uploads or close this form.
                </p>
              ) : null}
              {queuedDocuments.length > 0 ? (
                <div className="asset-create__documents" aria-label="Queued documents">
                  {queuedDocuments.map((doc) => (
                    <div className="asset-create__document" key={doc.localId}>
                      <div className="asset-create__document-file">
                        <strong>{doc.file.name}</strong>
                        <span>{formatFileSize(doc.file.size)}</span>
                      </div>
                      <div className="asset-create__grid">
                        <label className="field asset-create__field">
                          <span>Kind</span>
                          <select
                            value={doc.kind}
                            onChange={(event) =>
                              updateDocument(doc.localId, {
                                kind: event.target.value as DocumentKind,
                              })
                            }
                          >
                            {DOCUMENT_KINDS.map((option) => (
                              <option key={option.value} value={option.value}>
                                {option.label}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="field asset-create__field">
                          <span>Title</span>
                          <input
                            required
                            value={doc.title}
                            onChange={(event) =>
                              updateDocument(doc.localId, { title: event.target.value })
                            }
                          />
                        </label>
                      </div>
                      <button
                        type="button"
                        className="btn btn--ghost"
                        disabled={create.isPending}
                        onClick={() => removeDocument(doc.localId)}
                      >
                        Remove document
                      </button>
                    </div>
                  ))}
                </div>
              ) : null}
            </section>

            {areasQ.isError && (
              <p className="form-notice form-notice--error" role="alert">
                Areas could not load. You can save the asset without an area.
              </p>
            )}
            {formError && (
              <p id="asset-create-error" className="form-error" role="alert">
                {formError}
              </p>
            )}
          </div>

          <footer className="asset-create__footer">
            <button
              type="button"
              className="btn btn--ghost"
              disabled={create.isPending}
              onClick={() => dialogRef.current?.close()}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn--moss"
              disabled={create.isPending}
            >
              {submitLabel}
            </button>
          </footer>
        </form>
      </dialog>
    </>
  );
}

function optionalText(value: string): string | undefined {
  const trimmed = value.trim();
  return trimmed ? trimmed : undefined;
}

async function uploadQueuedAssetDocuments(
  asset: Asset,
  documents: QueuedAssetDocument[],
): Promise<AssetDocument[]> {
  const uploaded: AssetDocument[] = [];
  const failedDocuments: QueuedAssetDocument[] = [];
  for (const doc of documents) {
    try {
      uploaded.push(await uploadAssetDocument(asset.id, doc));
    } catch {
      failedDocuments.push(doc);
    }
  }
  if (failedDocuments.length > 0) {
    throw new AssetDocumentUploadError(asset, failedDocuments);
  }
  return uploaded;
}

function uploadAssetDocument(assetId: string, doc: QueuedAssetDocument): Promise<AssetDocument> {
  const body = new FormData();
  body.append("category", doc.kind);
  body.append("title", optionalText(doc.title) ?? doc.file.name);
  body.append("file", doc.file);
  return fetchJson<AssetDocument>(`/api/v1/assets/${assetId}/documents`, {
    method: "POST",
    body,
  });
}

function defaultDocumentKind(file: File): DocumentKind {
  if (file.type.startsWith("image/")) return "photo";
  const lowerName = file.name.toLowerCase();
  if (lowerName.includes("manual")) return "manual";
  if (lowerName.includes("warranty")) return "warranty";
  if (lowerName.includes("invoice")) return "invoice";
  if (lowerName.includes("receipt")) return "receipt";
  if (lowerName.includes("certificate")) return "certificate";
  if (lowerName.includes("contract")) return "contract";
  if (lowerName.includes("permit")) return "permit";
  if (lowerName.includes("insurance")) return "insurance";
  return "other";
}

function defaultDocumentTitle(file: File): string {
  return file.name.replace(/\.[^.]+$/, "").trim() || file.name;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function buildAssetCreateBody(input: {
  name: string;
  propertyId: string;
  assetTypeId: string;
  areaId: string;
  make: string;
  model: string;
  serialNumber: string;
  condition: AssetCondition;
  status: AssetStatus;
  installedOn: string;
  purchasedOn: string;
  purchasePrice: string;
  purchaseCurrency: string;
  purchaseVendor: string;
  warrantyExpiresOn: string;
  expectedLifespanYears: string;
  guestVisible: boolean;
  guestInstructions: string;
  notes: string;
}): AssetCreateBody | string {
  const name = input.name.trim();
  if (!name) return "Enter an asset name before creating the asset.";
  if (!input.propertyId) return "Choose a property for this asset.";

  const body: AssetCreateBody = {
    name,
    property_id: input.propertyId,
    condition: input.condition,
    status: input.status,
    guest_visible: input.guestVisible,
  };
  body.asset_type_id = optionalText(input.assetTypeId);
  body.area_id = optionalText(input.areaId);
  body.make = optionalText(input.make);
  body.model = optionalText(input.model);
  body.serial_number = optionalText(input.serialNumber);
  body.installed_on = optionalText(input.installedOn);
  body.purchased_on = optionalText(input.purchasedOn);
  body.purchase_vendor = optionalText(input.purchaseVendor);
  body.warranty_expires_on = optionalText(input.warrantyExpiresOn);
  body.guest_instructions_md = optionalText(input.guestInstructions);
  body.notes_md = optionalText(input.notes);

  const currency = optionalText(input.purchaseCurrency)?.toUpperCase();
  if (currency && !/^[A-Z]{3}$/.test(currency)) {
    return "Use a three-letter currency code, such as USD.";
  }
  body.purchase_currency = currency;

  const price = optionalText(input.purchasePrice);
  if (price) {
    const priceMatch = price.match(/^(\d+)(?:\.(\d{1,2}))?$/);
    if (!priceMatch) {
      return "Purchase price must be zero or more with up to two decimal places.";
    }
    body.purchase_price_cents =
      Number(priceMatch[1]) * 100 + Number((priceMatch[2] ?? "").padEnd(2, "0"));
  }

  const lifespan = optionalText(input.expectedLifespanYears);
  if (lifespan) {
    if (!/^[1-9]\d*$/.test(lifespan)) {
      return "Expected lifespan must be at least one year.";
    }
    const years = Number(lifespan);
    body.expected_lifespan_years = years;
  }

  return body;
}

function assetCreateErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const fieldMessages = error.fieldErrors
      .map((fieldError) => {
        const label = assetFieldLabel(fieldError.loc);
        const message = fieldError.msg?.trim();
        if (!message) return null;
        return label ? `${label}: ${message}` : message;
      })
      .filter((message): message is string => Boolean(message));
    if (fieldMessages.length > 0) {
      return "Could not create asset. " + fieldMessages.join(" ");
    }
    if (error.status === 422) {
      return error.detail ?? error.title ?? "Could not create asset. Check the fields and try again.";
    }
    return error.detail ?? error.title ?? "Could not create asset. Try again in a moment.";
  }
  if (error instanceof Error && error.message) return error.message;
  return "Could not create asset. Try again in a moment.";
}

function assetDocumentUploadErrorMessage(error: AssetDocumentUploadError): string {
  const names = error.failedDocuments.map((doc) => doc.file.name).join(", ");
  return `${error.message} Failed: ${names}. Retry the queued documents or close this form.`;
}

function assetFieldLabel(loc: readonly (string | number)[] | undefined): string | null {
  const field = loc?.at(-1);
  if (field === "name") return "Name";
  if (field === "property_id") return "Property";
  if (field === "area_id") return "Area";
  if (field === "asset_type_id") return "Type";
  if (field === "purchase_price_cents") return "Purchase price";
  if (field === "purchase_currency") return "Currency";
  if (field === "expected_lifespan_years") return "Expected lifespan";
  if (field === "guest_visible") return "Guest visibility";
  return null;
}

export default function AssetsPage() {
  // code-health: ignore[nloc] Assets page is query plus filterable card/table composition with shared controls.
  const { pathname } = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeCategory = searchParams.get("category") ?? "";
  const activeProperty = searchParams.get("property_id") ?? "";

  const assetsQ = useQuery({
    queryKey: qk.assets(),
    queryFn: () => fetchList<Asset>("/api/v1/assets"),
  });
  const typesQ = useQuery({
    queryKey: qk.assetTypes(),
    queryFn: () => fetchList<AssetType>("/api/v1/asset_types"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchList<Property>("/api/v1/properties"),
  });

  const sub = "Tracked equipment and appliances across all properties.";
  const actions = (
    <>
      <QrSheetButton category={activeCategory} propertyId={activeProperty} />
      <NewAssetButton
        assetTypes={typesQ.data}
        properties={propsQ.data}
        activePropertyId={activeProperty}
      />
    </>
  );

  if (assetsQ.isPending || typesQ.isPending || propsQ.isPending) {
    return <DeskPage title="Assets" sub={sub} actions={actions}><Loading /></DeskPage>;
  }
  if (!assetsQ.data || !typesQ.data || !propsQ.data) {
    return <DeskPage title="Assets" sub={sub} actions={actions}>Failed to load.</DeskPage>;
  }

  const typesById = new Map(typesQ.data.map((t) => [t.id, t]));
  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));

  const categories = Array.from(new Set(typesQ.data.map((t) => t.category)));

  const filtered = assetsQ.data.filter((a) => {
    if (activeProperty && a.property_id !== activeProperty) return false;
    if (activeCategory) {
      const at = a.asset_type_id ? typesById.get(a.asset_type_id) : null;
      if (!at || at.category !== activeCategory) return false;
    }
    return true;
  });

  const categoryOptions = categories.map((cat) => ({ value: cat, label: cat }));
  const propertyOptions = propsQ.data.map((p) => ({
    value: p.id,
    label: p.name,
    tone: p.color,
  }));
  const noAssets = assetsQ.data.length === 0;
  const noFilteredAssets = !noAssets && filtered.length === 0;

  return (
    <DeskPage title="Assets" sub={sub} actions={actions}>
      <section className="panel">
        <FilterChipGroup
          value={activeCategory}
          onChange={(value) =>
            setSearchParams(setSearchParam(searchParams, "category", value))
          }
          options={categoryOptions}
        />
        <FilterChipGroup
          value={activeProperty}
          onChange={(value) =>
            setSearchParams(setSearchParam(searchParams, "property_id", value))
          }
          allLabel="All properties"
          options={propertyOptions}
        />

        {noAssets ? (
          <EmptyState
            icon={PackageSearch}
            title="No assets listed yet"
            copy="Create an asset to track equipment, appliances, warranties, manuals, and QR labels."
            variant="compact"
          />
        ) : null}
        {noFilteredAssets ? (
          <EmptyState
            icon={SearchX}
            title="No assets match these filters"
            copy="Clear the active category or property filter to see the full asset list."
            variant="compact"
          />
        ) : null}
        {filtered.length > 0 ? (
          <table className="table">
            <thead>
              <tr>
                <th>Asset</th>
                <th>Type</th>
                <th>Property</th>
                <th>Area</th>
                <th>Condition</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((a) => {
                const at = a.asset_type_id ? typesById.get(a.asset_type_id) : null;
                const prop = propsById.get(a.property_id);
                const makeLine = [a.make, a.model].filter(Boolean).join(" ");
                return (
                  <tr key={a.id}>
                    <td>
                      <Link to={workspaceRouteForPathname(pathname, "/asset/" + a.id)} className="link asset-name-link">
                        {at && <AssetIcon name={at.icon_name} />}
                        <strong>{a.name}</strong>
                      </Link>
                      {makeLine && <span className="table__sub">{makeLine}</span>}
                    </td>
                    <td>{at?.name ?? <span className="muted">--</span>}</td>
                    <td>{prop && <Chip tone={prop.color} size="sm">{prop.name}</Chip>}</td>
                    <td>{a.area ?? <span className="muted">--</span>}</td>
                    <td><Chip tone={ASSET_CONDITION_TONE[a.condition]} size="sm">{a.condition.replace("_", " ")}</Chip></td>
                    <td><Chip tone={ASSET_STATUS_TONE[a.status]} size="sm">{a.status.replace("_", " ")}</Chip></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : null}
      </section>
    </DeskPage>
  );
}
