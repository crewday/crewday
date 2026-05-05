import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import AssetScanPage from "./AssetScanPage";
import { jsonResponse } from "@/test/helpers";

const zxing = vi.hoisted(() => ({
  decodeFromConstraints: vi.fn(),
  releaseAllStreams: vi.fn(),
}));

vi.mock("@zxing/browser", () => ({
  BrowserQRCodeReader: class {
    static releaseAllStreams = zxing.releaseAllStreams;
    decodeFromConstraints = zxing.decodeFromConstraints;
  },
}));

function AssetRouteProbe() {
  // code-health: ignore[nloc] Lizard misattributes the surrounding route harness body to this tiny probe.
  const { aid } = useParams<{ aid: string }>();
  return <p>Opened {aid}</p>;
}

interface HarnessProps {
  initial?: string;
}

function Harness({ initial = "/asset/scan" }: HarnessProps) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <WorkspaceProvider>
          <Routes>
            <Route path="/asset/scan" element={<AssetScanPage />} />
            <Route path="/asset/scan/:token" element={<AssetScanPage />} />
            <Route path="/asset/:aid" element={<AssetRouteProbe />} />
          </Routes>
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const originalMediaDevices = navigator.mediaDevices;

function setMediaDevices(value: MediaDevices | undefined): void {
  Object.defineProperty(navigator, "mediaDevices", {
    value,
    configurable: true,
  });
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
  zxing.decodeFromConstraints.mockReset();
  zxing.releaseAllStreams.mockReset();
  setMediaDevices(undefined);
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  setMediaDevices(originalMediaDevices);
  vi.restoreAllMocks();
});

describe("<AssetScanPage>", () => {
  it("falls back to manual entry when camera scanning is unavailable", async () => {
    const originalFetch = globalThis.fetch;
    const requests: string[] = [];
    const fetchSpy = vi.fn(async (url: string | URL | Request) => {
      const resolved = typeof url === "string" ? url : url.toString();
      requests.push(resolved);
      return jsonResponse({ id: "asset_1" });
    });
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;
    try {
      render(<Harness />);
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Camera scanning is not available on this device.",
      );
      expect(screen.getByText("Enter the QR code printed on the asset label")).toBeInTheDocument();
      fireEvent.change(screen.getByLabelText("QR code"), {
        target: { value: "https://crew.day/asset/scan/QR1234567890" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Open asset" }));
      await screen.findByText("Opened asset_1");
      expect(requests).toEqual(["/w/acme/api/v1/asset/scan/QR1234567890"]);
    } finally {
      (globalThis as { fetch: typeof fetch }).fetch = originalFetch;
    }
  });

  it("surfaces an unregistered asset notice without navigating", async () => {
    const originalFetch = globalThis.fetch;
    const fetchSpy = vi.fn(async () => jsonResponse({ detail: "missing" }, 404));
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;
    try {
      render(<Harness />);
      await screen.findByLabelText("QR code");
      fireEvent.change(screen.getByLabelText("QR code"), { target: { value: "ABCDEFGHJKM1" } });
      fireEvent.click(screen.getByRole("button", { name: "Open asset" }));
      await waitFor(() => {
        expect(screen.getByRole("alert")).toHaveTextContent(
          "This asset is not registered here.",
        );
      });
      expect(screen.queryByText(/Opened /)).toBeNull();
    } finally {
      (globalThis as { fetch: typeof fetch }).fetch = originalFetch;
    }
  });

  it("does not call the API for malformed QR text", async () => {
    const originalFetch = globalThis.fetch;
    const fetchSpy = vi.fn();
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;
    try {
      render(<Harness />);
      await screen.findByLabelText("QR code");
      fireEvent.change(screen.getByLabelText("QR code"), { target: { value: "https://crew.day/not-an-asset" } });
      fireEvent.click(screen.getByRole("button", { name: "Open asset" }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Enter the 12-character QR code printed on the asset label.",
      );
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally {
      (globalThis as { fetch: typeof fetch }).fetch = originalFetch;
    }
  });

  it("falls back to manual entry when camera permission is denied", async () => {
    setMediaDevices({ getUserMedia: vi.fn() } as unknown as MediaDevices);
    zxing.decodeFromConstraints.mockRejectedValue(new DOMException("Denied", "NotAllowedError"));

    render(<Harness />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Camera access was blocked. Enter the QR code instead.",
    );
    expect(screen.getByLabelText("QR code")).toBeInTheDocument();
  });

  it("opens the scanned asset and stops the camera scanner", async () => {
    setMediaDevices({ getUserMedia: vi.fn() } as unknown as MediaDevices);
    const originalFetch = globalThis.fetch;
    const stop = vi.fn();
    const requests: string[] = [];
    zxing.decodeFromConstraints.mockImplementation(async (_constraints, _video, callback) => {
      callback({ getText: () => "https://crew.day/asset/scan/qr1234567890" }, undefined, { stop });
      return { stop };
    });
    (globalThis as { fetch: typeof fetch }).fetch = vi.fn(async (url: string | URL | Request) => {
      requests.push(typeof url === "string" ? url : url.toString());
      return jsonResponse({ id: "asset_2" });
    }) as unknown as typeof fetch;
    try {
      render(<Harness />);
      await screen.findByText("Opened asset_2");
      expect(stop).toHaveBeenCalledTimes(1);
      expect(requests).toEqual(["/w/acme/api/v1/asset/scan/QR1234567890"]);
    } finally {
      (globalThis as { fetch: typeof fetch }).fetch = originalFetch;
    }
  });

  it("resolves direct /asset/scan/:token links without enabling demo data", async () => {
    const originalFetch = globalThis.fetch;
    const requests: string[] = [];
    (globalThis as { fetch: typeof fetch }).fetch = vi.fn(async (url: string | URL | Request) => {
      requests.push(typeof url === "string" ? url : url.toString());
      return jsonResponse({ id: "asset_3" });
    }) as unknown as typeof fetch;
    try {
      render(<Harness initial="/asset/scan/QR1234567890" />);
      await screen.findByText("Opened asset_3");
      expect(zxing.decodeFromConstraints).not.toHaveBeenCalled();
      expect(requests).toEqual(["/w/acme/api/v1/asset/scan/QR1234567890"]);
    } finally {
      (globalThis as { fetch: typeof fetch }).fetch = originalFetch;
    }
  });
});
