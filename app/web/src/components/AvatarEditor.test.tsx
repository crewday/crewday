import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import { installFetch } from "@/test/helpers";
import AvatarEditor from "./AvatarEditor";

const originalShowModal = HTMLDialogElement.prototype.showModal;
const originalClose = HTMLDialogElement.prototype.close;

function renderEditor(props: {
  currentUrl?: string | null;
  onClose?: () => void;
} = {}): void {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <AvatarEditor
        open
        onClose={props.onClose ?? vi.fn()}
        currentUrl={props.currentUrl ?? null}
        userName="Maya Singh"
      />
    </QueryClientProvider>,
  );
}

class LoadedImage {
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  naturalWidth = 640;
  naturalHeight = 640;

  set src(_value: string) {
    queueMicrotask(() => this.onload?.());
  }

  decode(): Promise<void> {
    return Promise.resolve();
  }
}

beforeEach(() => {
  __resetApiProvidersForTests();
  registerWorkspaceSlugGetter(() => "dev");
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close() {
    this.open = false;
  };
  vi.stubGlobal("Image", LoadedImage);
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:avatar");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    fillStyle: "",
    fillRect: vi.fn(),
    drawImage: vi.fn(),
  } as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(function toBlob(callback) {
    callback(new Blob(["avatar"], { type: "image/webp" }));
  });
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  HTMLDialogElement.prototype.showModal = originalShowModal;
  HTMLDialogElement.prototype.close = originalClose;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("AvatarEditor", () => {
  it("uploads to the bare-host avatar endpoint when a workspace slug is active", async () => {
    const env = installFetch(() => ({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ avatar_url: "/api/v1/files/file_01/blob", user: {} }),
    } as Response));

    try {
      renderEditor();

      const input = document.querySelector<HTMLInputElement>("input[type='file']");
      expect(input).not.toBeNull();
      fireEvent.change(input!, {
        target: {
          files: [new File(["image"], "avatar.png", { type: "image/png" })],
        },
      });

      fireEvent.click(await screen.findByRole("button", { name: "Save" }));

      await waitFor(() => {
        const upload = env.calls.find((call) => call.init.method === "POST");
        expect(upload).toBeDefined();
        expect(upload?.url).toBe("/api/v1/me/avatar");
        expect(upload?.url).not.toContain("/w/dev/");
        expect(upload?.init.body).toBeInstanceOf(FormData);
      });
    } finally {
      env.restore();
    }
  });

  it("clears through the bare-host avatar endpoint when a workspace slug is active", async () => {
    const env = installFetch(() => ({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ avatar_url: null }),
    } as Response));

    try {
      renderEditor({ currentUrl: "/api/v1/files/file_01/blob" });

      fireEvent.click(screen.getByRole("button", { name: "Remove photo" }));

      await waitFor(() => {
        const clear = env.calls.find((call) => call.init.method === "DELETE");
        expect(clear).toBeDefined();
        expect(clear?.url).toBe("/api/v1/me/avatar");
        expect(clear?.url).not.toContain("/w/dev/");
      });
    } finally {
      env.restore();
    }
  });
});
