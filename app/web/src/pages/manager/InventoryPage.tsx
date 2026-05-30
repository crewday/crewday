import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useLocation, useParams } from "react-router-dom";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { ApiError, fetchJson } from "@/lib/api";
import { formatDecimal } from "@/lib/numberFormat";
import { qk } from "@/lib/queryKeys";
import { useCloseOnEscape } from "@/lib/useCloseOnEscape";
import DeskPage from "@/components/DeskPage";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import DateTime from "@/components/DateTime";
import FormModal, {
  FormModalField,
  FormModalGrid,
} from "@/components/FormModal";
import {
  InlineNoteField,
  InlineNumberField,
  InlineSelectField,
  InlineTableForm,
  type InlineTableColumn,
  type InlineTableRow,
} from "@/components/InlineTableForm";
import SearchableSelect, { type SearchableSelectOption } from "@/components/SearchableSelect";
import { Chip, Loading } from "@/components/common";
import type { Property } from "@/types/api";
import PropertyTabs from "./property/PropertyTabs";

type InventoryMovementReason =
  | "restock"
  | "consume"
  | "produce"
  | "waste"
  | "theft"
  | "loss"
  | "found"
  | "returned_to_vendor"
  | "transfer_in"
  | "transfer_out"
  | "audit_correction"
  | "adjust";

interface InventoryMovement {
  id: string;
  item_id: string;
  delta: number;
  reason: InventoryMovementReason;
  actor_kind: "user" | "agent" | "system";
  actor_id: string | null;
  note: string | null;
  occurred_at: string;
  source_task_id: string | null;
  source_stocktake_id: string | null;
}

interface InventoryItem {
  id: string;
  property_id: string;
  name: string;
  sku: string;
  on_hand: number;
  par: number;
  unit: string;
  area: string;
  reorder_target: number | null;
}

interface WireInventoryItem {
  id: string;
  property_id: string;
  name: string;
  sku: string | null;
  on_hand: number;
  unit: string;
  reorder_point: number | null;
  reorder_target: number | null;
  tags: string[];
}

interface InventoryItemCreateBody {
  name: string;
  sku: string | null;
  unit: string;
  reorder_point: number;
  reorder_target: number | null;
  barcode_ean13: string | null;
}

interface NewInventoryItemDraft {
  propertyId: string;
  name: string;
  unit: string;
  sku: string;
  barcode: string;
  reorderPoint: string;
  reorderTarget: string;
}

interface NewInventoryItemState {
  draft: NewInventoryItemDraft;
  clientErr: string | null;
  serverErr: string | null;
}

type NewInventoryItemAction =
  | { type: "patch"; patch: Partial<NewInventoryItemDraft> }
  | { type: "clientError"; error: string | null }
  | { type: "serverError"; error: string | null }
  | { type: "reset"; properties: Property[] };

interface InventoryDrawerState {
  itemId: string;
  itemOnHand: number;
  itemPar: number;
  itemReorderTarget: number | null;
  observed: string;
  reason: InventoryMovementReason;
  note: string;
  err: string | null;
  reorderPoint: string;
  reorderTarget: string;
  reorderErr: string | null;
}

type InventoryDrawerAction =
  | { type: "syncItem"; item: InventoryItem }
  | { type: "patch"; patch: Partial<Pick<InventoryDrawerState, "observed" | "reason" | "note" | "reorderPoint" | "reorderTarget">> }
  | { type: "adjustError"; error: string | null }
  | { type: "reorderError"; error: string | null }
  | { type: "adjustSuccess" }
  | { type: "reorderSuccess" };

interface ListEnvelope<T> {
  data: T[];
}

function unwrapList<T>(payload: T[] | ListEnvelope<T>): T[] {
  return Array.isArray(payload) ? payload : payload.data;
}

async function fetchList<T>(path: string): Promise<T[]> {
  return unwrapList(await fetchJson<T[] | ListEnvelope<T>>(path));
}

function toInventoryItem(item: WireInventoryItem): InventoryItem {
  return {
    id: item.id,
    property_id: item.property_id,
    name: item.name,
    sku: item.sku ?? ",",
    on_hand: item.on_hand,
    par: item.reorder_point ?? 0,
    unit: item.unit,
    area: item.tags[0] ?? "General",
    reorder_target: item.reorder_target,
  };
}

function propertySelectOption(property: Property): SearchableSelectOption {
  return {
    value: property.id,
    label: property.name,
    secondaryText: property.city,
    searchText: `${property.name} ${property.city} ${property.timezone}`,
  };
}

function initialNewInventoryItemState(properties: Property[]): NewInventoryItemState {
  return {
    draft: {
      propertyId: properties[0]?.id ?? "",
      name: "",
      unit: "each",
      sku: "",
      barcode: "",
      reorderPoint: "0",
      reorderTarget: "",
    },
    clientErr: null,
    serverErr: null,
  };
}

function newInventoryItemReducer(
  state: NewInventoryItemState,
  action: NewInventoryItemAction,
): NewInventoryItemState {
  switch (action.type) {
    case "patch":
      return {
        ...state,
        draft: { ...state.draft, ...action.patch },
      };
    case "clientError":
      return { ...state, clientErr: action.error };
    case "serverError":
      return { ...state, serverErr: action.error };
    case "reset":
      return initialNewInventoryItemState(action.properties);
  }
}

function initialInventoryDrawerState(item: InventoryItem): InventoryDrawerState {
  return {
    itemId: item.id,
    itemOnHand: item.on_hand,
    itemPar: item.par,
    itemReorderTarget: item.reorder_target,
    observed: String(item.on_hand),
    reason: "audit_correction",
    note: "",
    err: null,
    reorderPoint: String(item.par),
    reorderTarget: String(item.reorder_target ?? item.par),
    reorderErr: null,
  };
}

function syncInventoryDrawerState(state: InventoryDrawerState, item: InventoryItem): InventoryDrawerState {
  if (
    state.itemId === item.id &&
    state.itemOnHand === item.on_hand &&
    state.itemPar === item.par &&
    state.itemReorderTarget === item.reorder_target
  ) {
    return state;
  }
  return {
    ...state,
    itemId: item.id,
    itemOnHand: item.on_hand,
    itemPar: item.par,
    itemReorderTarget: item.reorder_target,
    observed: String(item.on_hand),
    reorderPoint: String(item.par),
    reorderTarget: String(item.reorder_target ?? item.par),
    err: null,
    reorderErr: null,
  };
}

function inventoryDrawerReducer(
  state: InventoryDrawerState,
  action: InventoryDrawerAction,
): InventoryDrawerState {
  switch (action.type) {
    case "syncItem":
      return syncInventoryDrawerState(state, action.item);
    case "patch":
      return { ...state, ...action.patch };
    case "adjustError":
      return { ...state, err: action.error };
    case "reorderError":
      return { ...state, reorderErr: action.error };
    case "adjustSuccess":
      return { ...state, err: null, note: "" };
    case "reorderSuccess":
      return { ...state, reorderErr: null };
  }
}

async function fetchInventoryForProperties(properties: Property[]): Promise<InventoryItem[]> {
  const lists = await Promise.all(
    properties.map((property) =>
      fetchList<WireInventoryItem>(
        `/api/v1/inventory/properties/${property.id}/items`,
      ),
    ),
  );
  return lists.flat().map(toInventoryItem);
}

function makeIdempotencyKey(prefix: string): string {
  const random =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}:${random}`;
}

// §08, the reason vocabulary used by both the adjust drawer and
// the stocktake sheet. Kept narrow and intentional: each entry is a
// real-world story ("theft" ≠ "loss") so reports stay meaningful.
const ADJUST_REASONS: { value: InventoryMovementReason; label: string }[] = [
  { value: "audit_correction", label: "Audit correction (no cause)" },
  { value: "theft", label: "Theft" },
  { value: "loss", label: "Loss (unknown)" },
  { value: "found", label: "Found" },
  { value: "waste", label: "Waste / damaged" },
  { value: "returned_to_vendor", label: "Returned to vendor" },
  { value: "restock", label: "Restock (off-channel purchase)" },
];

const REASON_LABEL: Record<InventoryMovementReason, string> = {
  restock: "Restock",
  consume: "Consumed by task",
  produce: "Produced by task",
  waste: "Waste",
  theft: "Theft",
  loss: "Loss",
  found: "Found",
  returned_to_vendor: "Returned",
  transfer_in: "Transfer in",
  transfer_out: "Transfer out",
  audit_correction: "Audit correction",
  adjust: "Adjust",
};

// Each reason maps to a timeline dot variant. The ledger aesthetic
// stays quiet, moss for gains, rust for losses, ink for neutrals.
const REASON_TONE: Record<
  InventoryMovementReason,
  "gain" | "loss" | "neutral"
> = {
  restock: "gain",
  produce: "gain",
  found: "gain",
  transfer_in: "gain",
  consume: "loss",
  waste: "loss",
  theft: "loss",
  loss: "loss",
  returned_to_vendor: "loss",
  transfer_out: "loss",
  audit_correction: "neutral",
  adjust: "neutral",
};

// Format decimal qty with up to 3 decimals, trailing zeros trimmed.
// `2` stays `2`, `0.300` becomes `0.3`. Shared across the drawer,
// templates page, and task detail panel.
function fmtQty(n: number): string {
  // code-health: ignore[ccn] Decimal quantity formatter is over-counted by lizard after TSX parser recovery.
  if (!Number.isFinite(n)) return String(n);
  return formatDecimal(n, { maximumFractionDigits: 3 });
}

function roundedStockDelta(observed: number, onHand: number): number {
  // Arithmetic rounding for mutation payloads, not display formatting.
  return Number((observed - onHand).toFixed(4));
}

function errorCopy(error: Error, fallback: string): string {
  if (error instanceof ApiError) {
    const field = error.problem?.field;
    const code = error.problem?.error;
    if (code === "inventory_item_conflict" && field === "sku") {
      return "SKU already exists for this property.";
    }
    if (code === "inventory_item_conflict" && field === "barcode_ean13") {
      return "Barcode already exists for this property.";
    }
    if (code === "required" && field === "name") return "Name is required.";
    if (code === "blank" && field === "name") return "Name is required.";
    if (code === "required" && field === "unit") return "Unit is required.";
    if (code === "blank" && field === "unit") return "Unit is required.";
    return error.detail ?? error.title ?? error.message ?? fallback;
  }
  return error.message || fallback;
}

interface MovementsPage {
  items: InventoryMovement[];
  next_cursor: string | null;
}

interface WireMovementsPage {
  data: InventoryMovement[];
  next_cursor: string | null;
}

export default function InventoryPage() {
  // code-health: ignore[ccn nloc] Inventory route keeps filters, inline adjustment dialog, and table actions together.
  const qc = useQueryClient();
  const { pid } = useParams<{ pid?: string }>();
  const routePropertyId = pid ?? null;
  const { pathname } = useLocation();
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchList<Property>("/api/v1/properties"),
  });
  const pageProperties = useMemo(
    () => {
      if (!propsQ.data) return undefined;
      return routePropertyId
        ? propsQ.data.filter((property) => property.id === routePropertyId)
        : propsQ.data;
    },
    [propsQ.data, routePropertyId],
  );
  const inventoryQueryKey = routePropertyId
    ? ([...qk.inventory(), "property", routePropertyId] as const)
    : qk.inventory();
  const invQ = useQuery({
    queryKey: inventoryQueryKey,
    queryFn: () => fetchInventoryForProperties(pageProperties ?? []),
    enabled: pageProperties !== undefined,
  });

  const [openItemId, setOpenItemId] = useState<string | null>(null);
  const [stocktakePid, setStocktakePid] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const stocktakeRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    if (!stocktakePid) return;
    const dialog = stocktakeRef.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, [stocktakePid]);

  function closeStocktake() {
    stocktakeRef.current?.close();
    setStocktakePid(null);
  }

  const sub =
    "Per-property stock. Items at or below par trigger a procurement task. Click a row to see full history and adjust.";
  const actions = (
    <button
      type="button"
      className="btn btn--moss"
      onClick={() => setCreating(true)}
    >
      + New item
    </button>
  );
  const overflow = [
    {
      label: "Export CSV",
      onSelect: () => undefined,
      disabledReason: "Inventory export needs a specified API endpoint first.",
    },
  ];

  if (propsQ.isPending || (pageProperties !== undefined && invQ.isPending)) {
    return (
      <DeskPage title="Inventory" sub={sub} actions={actions} overflow={overflow}>
        <Loading />
      </DeskPage>
    );
  }
  if (!invQ.data || !propsQ.data || !pageProperties) {
    return (
      <DeskPage title="Inventory" sub={sub} actions={actions} overflow={overflow}>
        Failed to load.
      </DeskPage>
    );
  }

  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));
  const order: string[] = routePropertyId ? pageProperties.map((property) => property.id) : [];
  const byProp = new Map<string, InventoryItem[]>();
  for (const item of invQ.data) {
    if (!byProp.has(item.property_id)) {
      byProp.set(item.property_id, []);
      if (!routePropertyId) order.push(item.property_id);
    }
    byProp.get(item.property_id)!.push(item);
  }

  const openItem = openItemId
    ? invQ.data.find((i) => i.id === openItemId) ?? null
    : null;

  function startStocktake(pid: string) {
    setStocktakePid(pid);
    qc.invalidateQueries({ queryKey: qk.inventory() });
  }

  return (
    <DeskPage title="Inventory" sub={sub} actions={actions} overflow={overflow}>
      {routePropertyId ? (
        <PropertyTabs
          pathname={pathname}
          propertyId={routePropertyId}
          activeRelatedPage="inventory"
        />
      ) : null}

      {order.map((pid) => {
        const p = propsById.get(pid);
        const items = byProp.get(pid) ?? [];
        return (
          <div key={pid} className="panel">
            <header className="panel__head">
              <h2>
                {p && <Chip tone={p.color} size="sm">{p.name}</Chip>} Inventory
              </h2>
              <div className="inv-panel__actions">
                <span className="muted mono">{items.length} items</span>
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  onClick={() => startStocktake(pid)}
                >
                  Start stocktake
                </button>
              </div>
            </header>
            <table className="table inv-table">
              <thead>
                <tr>
                  <th>Item</th>
                  <th>SKU</th>
                  <th>Area</th>
                  <th className="num-col">On hand</th>
                  <th className="num-col">Par</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => {
                  const empty = item.on_hand <= 0;
                  const low = item.on_hand < item.par;
                  const active = openItemId === item.id;
                  const rowCls = [
                    "inv-row",
                    empty ? "row--critical" : low ? "row--warn" : "",
                    active ? "inv-row--active" : "",
                  ]
                    .filter(Boolean)
                    .join(" ");
                  return (
                    <tr
                      key={item.id}
                      className={rowCls}
                      onClick={() => setOpenItemId(item.id)}
                    >
                      <td><strong>{item.name}</strong></td>
                      <td className="mono muted">{item.sku}</td>
                      <td>{item.area}</td>
                      <td className="mono num-col">
                        <strong>{fmtQty(item.on_hand)}</strong>{" "}
                        <span className="unit">{item.unit}</span>
                      </td>
                      <td className="mono muted num-col">{fmtQty(item.par)}</td>
                      <td>
                        {empty ? (
                          <Chip tone="rust" size="sm">out of stock</Chip>
                        ) : low ? (
                          <Chip tone="sand" size="sm">below par</Chip>
                        ) : (
                          <Chip tone="moss" size="sm">ok</Chip>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        );
      })}

      {openItem && (
        <InventoryDrawer
          item={openItem}
          onClose={() => setOpenItemId(null)}
        />
      )}

      {/* Exemption: stocktake is a dense line-item operational sheet, not the compact structured record form covered by FormModal. */}
      <dialog
        ref={stocktakeRef}
        className="modal modal--sheet"
        onClose={() => setStocktakePid(null)}
      >
        {stocktakePid && (
          <StocktakeSheet
            propertyId={stocktakePid}
            propertyName={propsById.get(stocktakePid)?.name ?? ""}
            items={byProp.get(stocktakePid) ?? []}
            onClose={closeStocktake}
          />
        )}
      </dialog>

      {creating ? (
        <NewInventoryItemForm
          properties={pageProperties}
          onClose={() => setCreating(false)}
        />
      ) : null}
    </DeskPage>
  );
}

function NewInventoryItemForm({
  properties,
  onClose,
}: {
  properties: Property[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [state, dispatch] = useReducer(
    newInventoryItemReducer,
    properties,
    initialNewInventoryItemState,
  );
  const { draft, clientErr, serverErr } = state;
  const { propertyId, name, unit, sku, barcode, reorderPoint, reorderTarget } = draft;

  const create = useMutation({
    mutationFn: (body: InventoryItemCreateBody) =>
      fetchJson<WireInventoryItem>(
        `/api/v1/inventory/properties/${propertyId}/items`,
        {
          method: "POST",
          body,
        },
      ),
    onSuccess: async () => {
      dispatch({ type: "serverError", error: null });
      await qc.invalidateQueries({ queryKey: qk.inventory() });
      resetDraft();
      onClose();
    },
    onError: (error: Error) => {
      dispatch({ type: "serverError", error: errorCopy(error, "Item creation failed") });
    },
  });

  function resetDraft() {
    dispatch({ type: "reset", properties });
    create.reset();
  }

  function closeForm() {
    resetDraft();
    onClose();
  }

  function optionalText(value: string): string | null {
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : null;
  }

  const err = clientErr ?? serverErr;
  const errId = err ? "inventory-create-error" : undefined;
  const reorderPointHelpId = "inventory-create-reorder-point-help";
  const reorderTargetHelpId = "inventory-create-reorder-target-help";

  function describedBy(...ids: (string | undefined)[]): string | undefined {
    const present = ids.filter((id): id is string => id !== undefined);
    return present.length > 0 ? present.join(" ") : undefined;
  }

  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const trimmedName = name.trim();
    const trimmedUnit = unit.trim();
    const point = Number.parseFloat(reorderPoint);
    const target =
      reorderTarget.trim() === "" ? null : Number.parseFloat(reorderTarget);

    if (!propertyId) {
      dispatch({ type: "clientError", error: "Choose a property." });
      return;
    }
    if (!trimmedName) {
      dispatch({ type: "clientError", error: "Name is required." });
      return;
    }
    if (!trimmedUnit) {
      dispatch({ type: "clientError", error: "Unit is required." });
      return;
    }
    if (!Number.isFinite(point) || point < 0) {
      dispatch({ type: "clientError", error: "Reorder point must be zero or more." });
      return;
    }
    if (target !== null && (!Number.isFinite(target) || target < 0)) {
      dispatch({ type: "clientError", error: "Reorder target must be zero or more." });
      return;
    }
    if (target !== null && target < point) {
      dispatch({ type: "clientError", error: "Reorder target must be at least the reorder point." });
      return;
    }

    dispatch({ type: "clientError", error: null });
    dispatch({ type: "serverError", error: null });
    create.mutate({
      name: trimmedName,
      unit: trimmedUnit,
      sku: optionalText(sku),
      barcode_ean13: optionalText(barcode),
      reorder_point: point,
      reorder_target: target,
    });
  }

  return (
    <FormModal
      open
      title="Create item"
      titleId="inventory-create-title"
      eyebrow="New inventory item"
      width="narrow"
      className="inv-create-dialog"
      formClassName="inv-create"
      onClose={closeForm}
      onSubmit={submit}
      noValidate
      actions={
        <>
          <button type="button" className="btn btn--ghost" onClick={closeForm}>
            Cancel
          </button>
          <button type="submit" className="btn btn--moss" disabled={create.isPending}>
            Create item
          </button>
        </>
      }
    >
      {properties.length > 1 && (
        <SearchableSelect
          label="Property"
          className="form-modal__field"
          value={propertyId}
          options={properties.map(propertySelectOption)}
          onChange={(propertyId) => dispatch({ type: "patch", patch: { propertyId } })}
          required
          aria-invalid={clientErr === "Choose a property."}
          aria-describedby={errId}
        />
      )}
      <FormModalField label="Name" requirement="required">
        <input
          value={name}
          onChange={(e) => dispatch({ type: "patch", patch: { name: e.target.value } })}
          required
          aria-invalid={clientErr === "Name is required."}
          aria-describedby={errId}
         aria-label="Name"/>
      </FormModalField>
      <FormModalGrid>
        <FormModalField label="Unit" requirement="required">
          <input
            value={unit}
            onChange={(e) => dispatch({ type: "patch", patch: { unit: e.target.value } })}
            required
            list="inventory-unit-options"
            aria-invalid={clientErr === "Unit is required."}
            aria-describedby={errId}
           aria-label="Unit"/>
          <datalist id="inventory-unit-options">
            <option value="each">each</option>
            <option value="roll">roll</option>
            <option value="pack">pack</option>
            <option value="bottle">bottle</option>
            <option value="kg">kg</option>
            <option value="L">L</option>
          </datalist>
        </FormModalField>
        <FormModalField label="SKU" requirement="optional">
          <input
            value={sku}
            onChange={(e) => dispatch({ type: "patch", patch: { sku: e.target.value } })}
            aria-invalid={serverErr === "SKU already exists for this property."}
            aria-describedby={errId}
           aria-label="SKU"/>
        </FormModalField>
      </FormModalGrid>
      <FormModalField label="Barcode" requirement="optional">
        <input
          value={barcode}
          onChange={(e) => dispatch({ type: "patch", patch: { barcode: e.target.value } })}
          aria-invalid={serverErr === "Barcode already exists for this property."}
          aria-describedby={errId}
         aria-label="Barcode"/>
      </FormModalField>
      <FormModalGrid>
        <FormModalField
          label="Reorder point"
          requirement="required"
          helpId={reorderPointHelpId}
          helpText="Items at or below this threshold are low stock and can trigger procurement work."
        >
          <input
            className="mono"
            type="number"
            step="0.01"
            min="0"
            value={reorderPoint}
            onChange={(e) => dispatch({ type: "patch", patch: { reorderPoint: e.target.value } })}
            required
            aria-invalid={clientErr === "Reorder point must be zero or more."}
            aria-describedby={describedBy(reorderPointHelpId, errId)}
           aria-label="Reorder point"/>
        </FormModalField>
        <FormModalField
          label="Reorder target"
          requirement="optional"
          helpId={reorderTargetHelpId}
          helpText="Optional desired refill level; when provided, it must be at least the reorder point."
        >
          <input
            className="mono"
            type="number"
            step="0.01"
            min="0"
            value={reorderTarget}
            onChange={(e) => dispatch({ type: "patch", patch: { reorderTarget: e.target.value } })}
            aria-invalid={
              clientErr === "Reorder target must be zero or more." ||
              clientErr === "Reorder target must be at least the reorder point."
            }
            aria-describedby={describedBy(reorderTargetHelpId, errId)}
           aria-label="Reorder target"/>
        </FormModalField>
      </FormModalGrid>
      {err && (
        <p id="inventory-create-error" className="form-error" role="alert">
          {err}
        </p>
      )}
    </FormModal>
  );
}

// react-doctor-disable-next-line react-doctor/no-giant-component -- Existing promoted surface is intentionally deferred until a focused component split preserves behavior.
function InventoryDrawer({ item, onClose }: { item: InventoryItem; onClose: () => void }) {
  const qc = useQueryClient();
  useCloseOnEscape(onClose);

  // Infinite-scrolling ledger. 8 per page keeps the first screen
  // tight on laptops; IntersectionObserver fetches more when the
  // sentinel enters the drawer's own scroll viewport.
  const movementsQ = useInfiniteQuery<MovementsPage, Error>({
    queryKey: qk.inventoryMovements(item.id),
    initialPageParam: null,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: "8" });
      if (typeof pageParam === "string") params.set("before", pageParam);
      return fetchJson<WireMovementsPage>(
        `/api/v1/inventory/${item.id}/movements?${params.toString()}`,
      ).then((page) => ({
        items: page.data,
        next_cursor: page.next_cursor,
      }));
    },
    getNextPageParam: (last) => last.next_cursor,
  });

  const allMovements = useMemo(
    () => movementsQ.data?.pages.flatMap((p) => p.items) ?? [],
    [movementsQ.data],
  );

  // Drawer-scoped scroll root for the IntersectionObserver. We
  // observe the sentinel relative to the aside's own scroll viewport
  // so the trigger fires even when the drawer's inner overflow (not
  // the window) is what's scrolling.
  const drawerRef = useRef<HTMLElement | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    const root = drawerRef.current;
    if (!sentinel || !root) return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (
            entry.isIntersecting &&
            movementsQ.hasNextPage &&
            !movementsQ.isFetchingNextPage
          ) {
            void movementsQ.fetchNextPage();
          }
        }
      },
      { root, rootMargin: "140px" },
    );
    io.observe(sentinel);
    return () => io.disconnect();
  }, [movementsQ]);

  const [drawerState, dispatchDrawer] = useReducer(
    inventoryDrawerReducer,
    item,
    initialInventoryDrawerState,
  );
  let activeDrawerState = drawerState;
  const syncedDrawerState = syncInventoryDrawerState(drawerState, item);
  if (syncedDrawerState !== drawerState) {
    activeDrawerState = syncedDrawerState;
    dispatchDrawer({ type: "syncItem", item });
  }
  const { observed, reason, note, err, reorderPoint, reorderTarget, reorderErr } = activeDrawerState;

  const adjust = useMutation({
    mutationFn: (body: {
      observed_on_hand: number;
      reason: InventoryMovementReason;
      note: string;
    }) =>
      fetchJson<unknown>(`/api/v1/inventory/${item.id}/adjust`, {
        method: "POST",
        body,
      }),
    onSuccess: () => {
      dispatchDrawer({ type: "adjustSuccess" });
      qc.invalidateQueries({ queryKey: qk.inventory() });
      qc.invalidateQueries({ queryKey: qk.inventoryMovements(item.id) });
    },
    onError: (e: Error) => dispatchDrawer({ type: "adjustError", error: e.message || "Adjust failed" }),
  });

  const observedNum = Number.parseFloat(observed);
  const delta = Number.isFinite(observedNum)
    ? roundedStockDelta(observedNum, item.on_hand)
    : null;

  const coverage = item.par > 0 ? Math.min(1, item.on_hand / item.par) : 0;
  const statusLabel =
    item.on_hand <= 0 ? "out of stock" : item.on_hand < item.par ? "below par" : "in stock";
  const statusTone: "rust" | "sand" | "moss" =
    item.on_hand <= 0 ? "rust" : item.on_hand < item.par ? "sand" : "moss";
  const reorder = useMutation({
    mutationFn: (body: { reorder_point: number; reorder_target: number }) =>
      fetchJson<WireInventoryItem>(
        `/api/v1/inventory/properties/${item.property_id}/items/${item.id}`,
        {
          method: "PATCH",
          body,
        },
      ),
    onSuccess: () => {
      dispatchDrawer({ type: "reorderSuccess" });
      qc.invalidateQueries({ queryKey: qk.inventory() });
    },
    onError: (e: Error) => dispatchDrawer({ type: "reorderError", error: e.message || "Reorder update failed" }),
  });

  const reorderPointNum = Number.parseFloat(reorderPoint);
  const reorderTargetNum = Number.parseFloat(reorderTarget);
  const reorderChanged =
    Number.isFinite(reorderPointNum) &&
    Number.isFinite(reorderTargetNum) &&
    (Math.abs(reorderPointNum - item.par) > 1e-9 ||
      Math.abs(reorderTargetNum - (item.reorder_target ?? item.par)) > 1e-9);

  return (
    <>
      <div
        className="inv-drawer__scrim"
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        ref={drawerRef}
        className="inv-drawer"
        role="dialog"
        aria-label={`Inventory ledger, ${item.name}`}
      >
        <div className="inv-drawer__ribbon" aria-hidden="true" />
        <header className="inv-drawer__head">
          <div className="inv-drawer__head-top">
            <span className="inv-drawer__eyebrow">{item.sku}</span>
            <button
              type="button"
              className="inv-drawer__close"
              onClick={onClose}
              aria-label="Close (Esc)"
            >
              ×
            </button>
          </div>
          <h3 className="inv-drawer__title">{item.name}</h3>
          <div className="inv-drawer__meta">
            <span>{item.area}</span>
            <span className="inv-drawer__meta-sep" aria-hidden="true">·</span>
            <Chip tone={statusTone} size="sm">{statusLabel}</Chip>
          </div>
        </header>

        <section className="inv-hero">
          <div className="inv-hero__stat">
            <span className="inv-hero__label">On hand</span>
            <span className="inv-hero__num">{fmtQty(item.on_hand)}</span>
            <span className="inv-hero__unit">{item.unit}</span>
          </div>
          <div className="inv-hero__divider" aria-hidden="true" />
          <div className="inv-hero__stat inv-hero__stat--muted">
            <span className="inv-hero__label">Par</span>
            <span className="inv-hero__num">{fmtQty(item.par)}</span>
            <span className="inv-hero__unit">{item.unit}</span>
          </div>
          <div
            className="inv-hero__gauge"
            aria-hidden="true"
          >
            <div
              className={`inv-hero__gauge-fill inv-hero__gauge-fill--${statusTone}`}
              style={{ width: `${coverage * 100}%` }}
            />
          </div>
        </section>

        <section className="inv-drawer__adjust">
          <h4 className="inv-drawer__sect">Reorder rule</h4>
          <form
            className="inv-adjust"
            onSubmit={(e) => {
              e.preventDefault();
              if (
                !Number.isFinite(reorderPointNum) ||
                !Number.isFinite(reorderTargetNum)
              ) {
                dispatchDrawer({ type: "reorderError", error: "Par and target must be numbers." });
                return;
              }
              if (reorderPointNum < 0 || reorderTargetNum < 0) {
                dispatchDrawer({ type: "reorderError", error: "Par and target cannot be negative." });
                return;
              }
              if (reorderTargetNum < reorderPointNum) {
                dispatchDrawer({ type: "reorderError", error: "Target must be at least par." });
                return;
              }
              if (
                reorderPointNum < item.on_hand &&
                reorderPointNum < item.par &&
                !window.confirm(
                  "Lowering par below current stock can pause automatic restock tasks. Save anyway?",
                )
              ) {
                return;
              }
              reorder.mutate({
                reorder_point: reorderPointNum,
                reorder_target: reorderTargetNum,
              });
            }}
          >
            <div className="inv-adjust__grid">
              <label className="field inv-adjust__field">
                <span>Par</span>
                <div className="inv-adjust__input-row">
                  <input
                    className="inv-adjust__input mono"
                    type="number"
                    step="0.01"
                    min="0"
                    value={reorderPoint}
                    onChange={(e) => dispatchDrawer({ type: "patch", patch: { reorderPoint: e.target.value } })}
                    required
                   aria-label="field inv-adjust__field Par inv-adjust__input-row inv-adjust__input mono number 0.01 0 inv-adjust__unit"/>
                  <span className="inv-adjust__unit">{item.unit}</span>
                </div>
              </label>
              <label className="field inv-adjust__field">
                <span>Target</span>
                <div className="inv-adjust__input-row">
                  <input
                    className="inv-adjust__input mono"
                    type="number"
                    step="0.01"
                    min="0"
                    value={reorderTarget}
                    onChange={(e) => dispatchDrawer({ type: "patch", patch: { reorderTarget: e.target.value } })}
                    required
                   aria-label="field inv-adjust__field Target inv-adjust__input-row inv-adjust__input mono number 0.01 0 inv-adjust__unit"/>
                  <span className="inv-adjust__unit">{item.unit}</span>
                </div>
              </label>
            </div>
            <div className="inv-adjust__footer">
              <span className="muted">
                Restock tasks trigger when on-hand reaches par.
              </span>
              <button
                className="btn btn--moss"
                type="submit"
                disabled={reorder.isPending || !reorderChanged}
              >
                Save reorder rule
              </button>
            </div>
            {reorderErr && <p className="form-error">{reorderErr}</p>}
          </form>
        </section>

        <section className="inv-drawer__adjust">
          <h4 className="inv-drawer__sect">Record an event</h4>
          <form
            className="inv-adjust"
            onSubmit={(e) => {
              e.preventDefault();
              if (delta === null || delta === 0) {
                dispatchDrawer({ type: "adjustError", error: "Observed must differ from current on-hand." });
                return;
              }
              adjust.mutate({
                observed_on_hand: observedNum,
                reason,
                note,
              });
            }}
          >
            <div className="inv-adjust__grid">
              <label className="field inv-adjust__field">
                <span>Observed count</span>
                <div className="inv-adjust__input-row">
                  <input
                    className="inv-adjust__input mono"
                    type="number"
                    step="0.01"
                    min="0"
                    value={observed}
                    onChange={(e) => dispatchDrawer({ type: "patch", patch: { observed: e.target.value } })}
                    required
                   aria-label="field inv-adjust__field Observed count inv-adjust__input-row inv-adjust__input mono number 0.01 0 inv-adjust__unit"/>
                  <span className="inv-adjust__unit">{item.unit}</span>
                </div>
              </label>
              <label className="field inv-adjust__field">
                <span>Reason</span>
                <select
                  className="inv-adjust__select"
                  value={reason}
                  onChange={(e) =>
                    dispatchDrawer({ type: "patch", patch: { reason: e.target.value as InventoryMovementReason } })
                  }
                >
                  {ADJUST_REASONS.map((r) => (
                    <option key={r.value} value={r.value}>
                      {r.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="field">
              <span>Note (optional)</span>
              <AutoGrowTextarea
                className="inv-adjust__note"
                aria-label="Note (optional)"
                placeholder="e.g. Found in garage, soaked in rain."
                value={note}
                onChange={(e) => dispatchDrawer({ type: "patch", patch: { note: e.target.value } })}
              />
            </div>
            <div className="inv-adjust__footer">
              <div
                className={`inv-delta inv-delta--${
                  delta === null || delta === 0
                    ? "neutral"
                    : delta > 0
                      ? "gain"
                      : "loss"
                }`}
                aria-live="polite"
              >
                {delta === null || delta === 0 ? (
                  <>
                    <span className="inv-delta__sign">,</span>
                    <span className="inv-delta__body">no change yet</span>
                  </>
                ) : (
                  <>
                    <span className="inv-delta__sign">
                      {delta > 0 ? "+" : "−"}
                    </span>
                    <span className="inv-delta__num mono">
                      {fmtQty(Math.abs(delta))}
                    </span>
                    <span className="inv-delta__unit">{item.unit}</span>
                  </>
                )}
              </div>
              <button
                className="btn btn--moss"
                type="submit"
                disabled={adjust.isPending || delta === null || delta === 0}
              >
                Record adjustment
              </button>
            </div>
            {err && <p className="form-error">{err}</p>}
          </form>
        </section>

        <section className="inv-drawer__history">
          <header className="inv-drawer__history-head">
            <h4 className="inv-drawer__sect">Ledger</h4>
            {!movementsQ.isPending && (
              <span className="inv-drawer__history-count muted mono">
                {allMovements.length} entries
              </span>
            )}
          </header>
          {movementsQ.isPending ? (
            <Loading />
          ) : movementsQ.isError ? (
            <p className="form-error">Failed to load history.</p>
          ) : allMovements.length === 0 ? (
            <p className="muted inv-history__empty">
              No movements yet. The first restock or task completion shows up here.
            </p>
          ) : (
            <ol className="inv-history">
              {allMovements.map((m, idx) => {
                const tone = REASON_TONE[m.reason];
                return (
                  <li
                    key={m.id}
                    className={`inv-history__row inv-history__row--${tone}`}
                    style={{ animationDelay: `${Math.min(idx * 28, 320)}ms` }}
                  >
                    <div className="inv-history__dot" aria-hidden="true" />
                    <div className="inv-history__body">
                      <div className="inv-history__top">
                        <span className="inv-history__reason">
                          {REASON_LABEL[m.reason]}
                        </span>
                        <DateTime value={m.occurred_at} className="inv-history__when mono" />
                      </div>
                      <div
                        className={`inv-history__delta mono inv-history__delta--${tone}`}
                      >
                        {m.delta > 0 ? "+" : "−"}
                        {fmtQty(Math.abs(m.delta))} {item.unit}
                      </div>
                      <div className="inv-history__foot">
                        <span className="muted">{m.actor_id}</span>
                        {m.source_task_id && (
                          <span className="inv-history__src">
                            ↳ task {m.source_task_id}
                          </span>
                        )}
                        {m.source_stocktake_id && (
                          <span className="inv-history__src">
                            ↳ stocktake
                          </span>
                        )}
                        {m.note && (
                          <span className="inv-history__note">{m.note}</span>
                        )}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ol>
          )}
          <div ref={sentinelRef} className="inv-history__sentinel" aria-hidden="true" />
          {movementsQ.isFetchingNextPage && (
            <p className="inv-history__loading muted">
              <span className="inv-history__loading-dots" aria-hidden="true">
                <i /><i /><i />
              </span>
              loading older entries
            </p>
          )}
          {!movementsQ.hasNextPage && allMovements.length > 0 && !movementsQ.isFetchingNextPage && (
            <p className="inv-history__end muted">· end of ledger ·</p>
          )}
        </section>
      </aside>
    </>
  );
}

interface StocktakeLine {
  item_id: string;
  observed: string;
  reason: InventoryMovementReason;
  note: string;
}

function initialStocktakeLine(item: InventoryItem): StocktakeLine {
  return {
    item_id: item.id,
    observed: String(item.on_hand),
    reason: "audit_correction",
    note: "",
  };
}

function initialStocktakeLines(items: InventoryItem[]): Record<string, StocktakeLine> {
  return Object.fromEntries(
    items.map((item) => [item.id, initialStocktakeLine(item)]),
  );
}

function syncStocktakeLines(
  current: Record<string, StocktakeLine>,
  items: InventoryItem[],
): Record<string, StocktakeLine> {
  let changed = Object.keys(current).length !== items.length;
  const next: Record<string, StocktakeLine> = {};
  for (const item of items) {
    const existing = current[item.id];
    next[item.id] = existing ?? initialStocktakeLine(item);
    if (!existing) changed = true;
  }
  return changed ? next : current;
}

function parseStocktakeObserved(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const observedNum = Number(trimmed);
  return Number.isFinite(observedNum) ? observedNum : null;
}

function stocktakeObservedValidation(line: StocktakeLine): string | null {
  if (!line.observed.trim()) return null;
  const observedNum = parseStocktakeObserved(line.observed);
  if (observedNum === null) return "Enter a valid observed count.";
  if (observedNum < 0) return "Observed count must be 0 or more.";
  return null;
}

function stocktakeLineDelta(
  line: StocktakeLine,
  item: InventoryItem,
): number | null {
  const observedNum = parseStocktakeObserved(line.observed);
  return observedNum !== null && observedNum >= 0
    ? roundedStockDelta(observedNum, item.on_hand)
    : null;
}

function stocktakeLineChanged(line: StocktakeLine, item: InventoryItem): boolean {
  const initial = initialStocktakeLine(item);
  return line.observed !== initial.observed
    || line.reason !== initial.reason
    || line.note !== initial.note;
}

function isStocktakeCommitCandidate(
  line: StocktakeLine,
  item: InventoryItem,
): boolean {
  if (stocktakeObservedValidation(line)) return false;
  const delta = stocktakeLineDelta(line, item);
  return delta !== null && Math.abs(delta) > 1e-9;
}

// react-doctor-disable-next-line react-doctor/no-giant-component -- Existing promoted surface is intentionally deferred until a focused component split preserves behavior.
function StocktakeSheet({
  propertyId,
  propertyName,
  items,
  onClose,
}: {
  propertyId: string;
  propertyName: string;
  items: InventoryItem[];
  onClose: () => void;
}) {
  const qc = useQueryClient();

  const [lines, setLines] = useState<Record<string, StocktakeLine>>(() =>
    initialStocktakeLines(items),
  );
  const [err, setErr] = useState<string | null>(null);
  let activeLines = lines;
  const syncedLines = syncStocktakeLines(lines, items);
  if (syncedLines !== lines) {
    activeLines = syncedLines;
    setLines(syncedLines);
  }

  const open = useMutation({
    mutationFn: () =>
      fetchJson<{ id: string }>(
        `/api/v1/properties/${propertyId}/stocktakes`,
        { method: "POST", body: {} },
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.inventory(), refetchType: "none" });
    },
  });
  const commit = useMutation({
    mutationFn: (sid: string) =>
      fetchJson<unknown>(`/api/v1/stocktakes/${sid}/commit`, {
        method: "POST",
        headers: {
          "Idempotency-Key": makeIdempotencyKey(`stocktake:${sid}:commit`),
        },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.inventory() });
      onClose();
    },
  });

  const itemById = useMemo(
    () => new Map(items.map((item) => [item.id, item])),
    [items],
  );
  const commitCandidates = useMemo(
    () =>
      items.filter((i) => {
        const l = activeLines[i.id];
        if (!l) return false;
        return isStocktakeCommitCandidate(l, i);
      }),
    [items, activeLines],
  );
  const stocktakeRows = useMemo<InlineTableRow<StocktakeLine>[]>(
    () =>
      items.map((item) => {
        const draft = activeLines[item.id] ?? initialStocktakeLine(item);
        return {
          id: item.id,
          label: item.name,
          draft,
          editing: true,
          dirty: stocktakeLineChanged(draft, item),
          validation: stocktakeObservedValidation(draft),
          meta: (
            <span className="stocktake__row-meta">
              <span className="muted mono">{item.sku}</span>
              <span className="muted">{item.area}</span>
            </span>
          ),
        };
      }),
    [items, activeLines],
  );
  const stocktakeColumns = useMemo<InlineTableColumn<StocktakeLine>[]>(
    () => [
      {
        key: "item",
        header: "Item",
        width: { flex: 1.5, min: 180 },
        renderRead: ({ row }) => {
          const item = itemById.get(row.id);
          return item ? <strong>{item.name}</strong> : row.label;
        },
        renderEdit: ({ row }) => {
          const item = itemById.get(row.id);
          return (
            <span className="stocktake__item-cell">
              <strong>{item?.name ?? row.label}</strong>
              {item ? (
                <span className="muted mono">{item.sku}</span>
              ) : null}
            </span>
          );
        },
      },
      {
        key: "current",
        header: "Current",
        mobileLabel: "On hand / par",
        align: "end",
        width: { px: 126 },
        renderRead: ({ row }) => {
          const item = itemById.get(row.id);
          return item ? (
            <span className="stocktake__read-num mono">
              {fmtQty(item.on_hand)} / {fmtQty(item.par)}
            </span>
          ) : null;
        },
        renderEdit: ({ row }) => {
          const item = itemById.get(row.id);
          return item ? (
            <span className="stocktake__read-num mono">
              <strong>{fmtQty(item.on_hand)}</strong>
              <span className="muted"> / {fmtQty(item.par)}</span>
              <span className="unit"> {item.unit}</span>
            </span>
          ) : null;
        },
      },
      {
        key: "observed",
        header: "Observed",
        align: "end",
        width: { px: 112 },
        renderRead: ({ row }) => row.draft.observed,
        renderEdit: ({ row, update, disabled }) => {
          const item = itemById.get(row.id);
          return (
            <InlineNumberField
              value={row.draft.observed}
              min={0}
              step="0.01"
              disabled={disabled}
              ariaLabel={`${item?.name ?? row.label} observed count`}
              onChange={(observed) => update({ observed })}
            />
          );
        },
      },
      {
        key: "reason",
        header: "Reason",
        width: { flex: 1, min: 170 },
        renderRead: ({ row }) =>
          ADJUST_REASONS.find((reason) => reason.value === row.draft.reason)?.label ??
          row.draft.reason,
        renderEdit: ({ row, update, disabled }) => {
          const item = itemById.get(row.id);
          const delta = item ? stocktakeLineDelta(row.draft, item) : null;
          return (
            <InlineSelectField
              value={row.draft.reason}
              options={ADJUST_REASONS}
              disabled={disabled || delta === null || delta === 0}
              ariaLabel={`${item?.name ?? row.label} reason`}
              onChange={(reason) =>
                update({ reason: reason as InventoryMovementReason })
              }
            />
          );
        },
      },
      {
        key: "delta",
        header: "Delta",
        align: "end",
        width: { px: 82 },
        renderRead: ({ row }) => {
          const item = itemById.get(row.id);
          if (!item) return null;
          const delta = stocktakeLineDelta(row.draft, item);
          return <StocktakeDelta delta={delta} />;
        },
        renderEdit: ({ row }) => {
          const item = itemById.get(row.id);
          if (!item) return null;
          const delta = stocktakeLineDelta(row.draft, item);
          return <StocktakeDelta delta={delta} />;
        },
      },
    ],
    [itemById],
  );

  async function submit() {
    setErr(null);
    try {
      const session = await open.mutateAsync();
      const payload = {
        lines: commitCandidates.flatMap((i) => {
          // ``commitCandidates`` is filtered to items whose line exists;
          // re-narrow here because ``lines`` is an open Record.
          const l = activeLines[i.id];
          if (!l) return [];
          if (!isStocktakeCommitCandidate(l, i)) return [];
          const observed_on_hand = parseStocktakeObserved(l.observed);
          if (observed_on_hand === null) return [];
          return [
            {
              item_id: i.id,
              observed_on_hand,
              reason: l.reason,
              note: l.note,
            },
          ];
        }),
      };
      await Promise.all(
        payload.lines.map((line) =>
          fetchJson<unknown>(
            `/api/v1/stocktakes/${session.id}/lines/${line.item_id}`,
            {
              method: "PATCH",
              body: {
                observed_on_hand: line.observed_on_hand,
                reason: line.reason,
                note: line.note,
              },
            },
          ),
        ),
      );
      await commit.mutateAsync(session.id);
    } catch (e) {
      setErr((e as Error).message || "Stocktake failed");
    }
  }

  return (
    <form
      className="modal__body stocktake"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <h3 className="modal__title">Stocktake, {propertyName}</h3>
      <p className="modal__sub">
        Walk the property, enter observed counts, pick a reason for any
        drift, and commit. A single audit row ties the whole session
        together.
      </p>

      <InlineTableForm
        compact
        className="stocktake__table"
        ariaLabel={`Stocktake rows for ${propertyName}`}
        columns={stocktakeColumns}
        rows={stocktakeRows}
        saveMode="batch"
        onDraftChange={(rowId, patch) => {
          const item = itemById.get(rowId);
          if (!item) return;
          setErr(null);
          setLines((prev) => ({
            ...prev,
            [rowId]: {
              ...(prev[rowId] ?? initialStocktakeLine(item)),
              ...patch,
            },
          }));
        }}
        onBatchCancel={() => {
          setErr(null);
          setLines(initialStocktakeLines(items));
        }}
        getRowLabel={(row) => row.label ?? row.id}
        renderDetail={({ row, update, disabled }) => {
          const item = itemById.get(row.id);
          return (
            <InlineNoteField
              value={row.draft.note}
              disabled={disabled}
              ariaLabel={`${item?.name ?? row.label} note`}
              placeholder="Note for this line"
              onChange={(note) => update({ note })}
            />
          );
        }}
        renderBatchActions={({ canDiscard, canSubmit, discard }) => (
          <div className="stocktake__batch">
            {err && <p className="form-error">{err}</p>}
            <div className="modal__actions">
              <button
                type="button"
                className="btn btn--ghost"
                onClick={canDiscard ? discard : onClose}
                disabled={open.isPending || commit.isPending}
              >
                {canDiscard ? "Discard changes" : "Cancel"}
              </button>
              <button
                type="submit"
                className="btn btn--moss"
                disabled={
                  commitCandidates.length === 0
                    || !canSubmit
                    || open.isPending
                    || commit.isPending
                }
              >
                {commitCandidates.length === 0
                  ? "No changes to commit"
                  : `Commit ${commitCandidates.length} change${commitCandidates.length === 1 ? "" : "s"}`}
              </button>
            </div>
          </div>
        )}
      />
    </form>
  );
}

function StocktakeDelta({ delta }: { delta: number | null }) {
  if (delta === null || delta === 0) {
    return <span className="stocktake__delta mono muted">-</span>;
  }
  return (
    <span
      className={[
        "stocktake__delta",
        "mono",
        delta > 0 ? "delta-pos" : "delta-neg",
      ].join(" ")}
    >
      {delta > 0 ? "+" : ""}
      {fmtQty(delta)}
    </span>
  );
}
