// crewday — passkey-register helper unit tests.
//
// Pins the recovery-enrol ceremony helpers (cd-3x3t) so a regression
// in the JSON ↔ ArrayBuffer marshalling, the wire layout, or the
// platform-error mapping cannot ship silently.
//
// Mirror of `passkey.test.ts` (login flow): same `installFetch`
// scripted-fetch shape and `Object.defineProperty(navigator,
// 'credentials', ...)` stub pattern. We don't reach for a shared
// helper module because none of the existing tests do — duplicating
// these ~40 lines keeps each suite readable on its own.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  beginRecoveryEnroll,
  decodeCreationOptions,
  encodeAttestation,
  finishRecoveryEnroll,
  runRecoveryEnrollCeremony,
  verifyRecoveryToken,
} from "./passkey-register";
import {
  PasskeyCancelledError,
  PasskeyTimeoutError,
  PasskeyTransientError,
  PasskeyUnsupportedError,
} from "./passkey";
import { __resetApiProvidersForTests } from "@/lib/api";

// ── Helpers ───────────────────────────────────────────────────────

function bytes(...vals: number[]): ArrayBuffer {
  return new Uint8Array(vals).buffer;
}

function bufToB64Url(buf: ArrayBuffer): string {
  const bytesArr = new Uint8Array(buf);
  let s = "";
  for (const b of bytesArr) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

interface FakeResponse {
  status: number;
  body: unknown;
}

function installFetch(responses: FakeResponse[]): {
  calls: Array<{ url: string; init: RequestInit }>;
  restore: () => void;
} {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    const next = responses.shift();
    if (!next) throw new Error(`Unexpected fetch call: ${resolved}`);
    const ok = next.status >= 200 && next.status < 300;
    const text = next.body === undefined ? "" : JSON.stringify(next.body);
    return {
      ok,
      status: next.status,
      statusText: ok ? "OK" : "Error",
      text: async () => text,
    } as unknown as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

beforeEach(() => {
  __resetApiProvidersForTests();
});

afterEach(() => {
  __resetApiProvidersForTests();
  vi.unstubAllGlobals();
});

// ── decodeCreationOptions ─────────────────────────────────────────

describe("decodeCreationOptions — fallback path (no native parse helper)", () => {
  beforeEach(() => {
    // Force the manual decoder. jsdom omits `PublicKeyCredential`
    // entirely; the explicit stub keeps the test honest if a future
    // polyfill leaks one in.
    vi.stubGlobal("PublicKeyCredential", undefined);
  });

  it("decodes the challenge and user.id from base64url to ArrayBuffer", () => {
    const decoded = decodeCreationOptions({
      challenge: "AQID" /* 0x01 0x02 0x03 */,
      rp: { id: "crew.day", name: "crew.day" },
      user: { id: "AQIDBA" /* 0x01 0x02 0x03 0x04 */, name: "maria@example.com", displayName: "Maria" },
      pubKeyCredParams: [{ type: "public-key", alg: -7 }],
    });
    expect(decoded.challenge).toBeInstanceOf(ArrayBuffer);
    expect([...new Uint8Array(decoded.challenge as ArrayBuffer)]).toEqual([1, 2, 3]);
    expect(decoded.user.id).toBeInstanceOf(ArrayBuffer);
    expect([...new Uint8Array(decoded.user.id as ArrayBuffer)]).toEqual([1, 2, 3, 4]);
    // Scalar fields pass through untouched.
    expect(decoded.user.name).toBe("maria@example.com");
    expect(decoded.user.displayName).toBe("Maria");
    expect(decoded.rp.id).toBe("crew.day");
    expect(decoded.pubKeyCredParams).toEqual([{ type: "public-key", alg: -7 }]);
  });

  it("decodes excludeCredentials[].id and preserves transports", () => {
    const decoded = decodeCreationOptions({
      challenge: "AQID",
      rp: { name: "crew.day" },
      user: { id: "AQID", name: "u", displayName: "U" },
      pubKeyCredParams: [{ type: "public-key", alg: -7 }],
      excludeCredentials: [
        { id: "AQID", type: "public-key", transports: ["internal", "hybrid"] },
      ],
    });
    const list = decoded.excludeCredentials!;
    expect(list).toHaveLength(1);
    const item = list[0]!;
    expect(item.type).toBe("public-key");
    expect(item.transports).toEqual(["internal", "hybrid"]);
    expect(new Uint8Array(item.id as ArrayBuffer)).toEqual(new Uint8Array([1, 2, 3]));
  });

  it("passes through timeout, attestation, and authenticatorSelection", () => {
    const decoded = decodeCreationOptions({
      challenge: "AQID",
      rp: { name: "crew.day" },
      user: { id: "AQID", name: "u", displayName: "U" },
      pubKeyCredParams: [{ type: "public-key", alg: -7 }],
      timeout: 60_000,
      attestation: "none",
      authenticatorSelection: {
        residentKey: "required",
        userVerification: "required",
      },
    });
    expect(decoded.timeout).toBe(60_000);
    expect(decoded.attestation).toBe("none");
    expect(decoded.authenticatorSelection).toEqual({
      residentKey: "required",
      userVerification: "required",
    });
  });

  it("tolerates a server that emits padded base64 instead of base64url", () => {
    // Defensive parity with `decodeRequestOptions` (login flow):
    // server rollouts that forget to switch to base64url shouldn't
    // crash the SPA.
    const decoded = decodeCreationOptions({
      challenge: "+/8=",
      rp: { name: "crew.day" },
      user: { id: "+/8=", name: "u", displayName: "U" },
      pubKeyCredParams: [{ type: "public-key", alg: -7 }],
    });
    expect([...new Uint8Array(decoded.challenge as ArrayBuffer)]).toEqual([0xfb, 0xff]);
    expect([...new Uint8Array(decoded.user.id as ArrayBuffer)]).toEqual([0xfb, 0xff]);
  });
});

// ── encodeAttestation ─────────────────────────────────────────────

describe("encodeAttestation", () => {
  it("base64url-encodes rawId / clientDataJSON / attestationObject and forwards transports", () => {
    const credential = {
      id: "test-id",
      rawId: bytes(0xaa, 0xbb),
      type: "public-key",
      response: {
        clientDataJSON: bytes(0x02),
        attestationObject: bytes(0x03, 0x04),
        getTransports: () => ["internal", "hybrid"],
      },
      authenticatorAttachment: "platform" as const,
      getClientExtensionResults: () => ({}),
    } as unknown as PublicKeyCredential;

    const encoded = encodeAttestation(credential);
    expect(encoded.id).toBe("test-id");
    expect(encoded.rawId).toBe(bufToB64Url(bytes(0xaa, 0xbb)));
    expect(encoded.type).toBe("public-key");
    expect(encoded.response.clientDataJSON).toBe(bufToB64Url(bytes(0x02)));
    expect(encoded.response.attestationObject).toBe(bufToB64Url(bytes(0x03, 0x04)));
    expect(encoded.response.transports).toEqual(["internal", "hybrid"]);
    expect(encoded.authenticatorAttachment).toBe("platform");
  });

  it("omits transports when getTransports is missing or returns an empty list", () => {
    const noHelper = {
      id: "no-transports",
      rawId: bytes(0xaa),
      type: "public-key",
      response: {
        clientDataJSON: bytes(0x02),
        attestationObject: bytes(0x03),
        // getTransports intentionally absent — older authenticators.
      },
      getClientExtensionResults: () => ({}),
    } as unknown as PublicKeyCredential;
    const encodedA = encodeAttestation(noHelper);
    expect(encodedA.response.transports).toBeUndefined();

    const empty = {
      id: "empty-transports",
      rawId: bytes(0xaa),
      type: "public-key",
      response: {
        clientDataJSON: bytes(0x02),
        attestationObject: bytes(0x03),
        getTransports: () => [],
      },
      getClientExtensionResults: () => ({}),
    } as unknown as PublicKeyCredential;
    const encodedB = encodeAttestation(empty);
    expect(encodedB.response.transports).toBeUndefined();
  });

  it("forwards extension results when the authenticator returns any", () => {
    const credential = {
      id: "with-ext",
      rawId: bytes(0xaa),
      type: "public-key",
      response: {
        clientDataJSON: bytes(0x02),
        attestationObject: bytes(0x03),
      },
      getClientExtensionResults: () => ({ credProps: { rk: true } } as Record<string, unknown>),
    } as unknown as PublicKeyCredential;
    const encoded = encodeAttestation(credential);
    expect(encoded.clientExtensionResults).toEqual({ credProps: { rk: true } });
  });
});

// ── verifyRecoveryToken / beginRecoveryEnroll / finishRecoveryEnroll

describe("verifyRecoveryToken", () => {
  it("GETs /api/v1/recover/passkey/verify with a URL-encoded token and returns the session id", async () => {
    const { calls, restore } = installFetch([
      { status: 200, body: { recovery_session_id: "rs_1" } },
    ]);
    try {
      const out = await verifyRecoveryToken("abc def+/=");
      expect(out.recovery_session_id).toBe("rs_1");
      expect(calls).toHaveLength(1);
      // The token must be percent-encoded — special chars must not
      // leak into the query string raw.
      expect(calls[0]!.url).toBe(
        "/api/v1/recover/passkey/verify?token=abc%20def%2B%2F%3D",
      );
      expect(calls[0]!.init.method).toBe("GET");
    } finally {
      restore();
    }
  });
});

describe("beginRecoveryEnroll / finishRecoveryEnroll", () => {
  it("POSTs the recovery session id to /start and returns the challenge envelope", async () => {
    const { calls, restore } = installFetch([
      {
        status: 200,
        body: {
          challenge_id: "ch_1",
          options: {
            challenge: "AQID",
            rp: { name: "crew.day" },
            user: { id: "AQID", name: "u", displayName: "U" },
            pubKeyCredParams: [{ type: "public-key", alg: -7 }],
          },
        },
      },
    ]);
    try {
      const out = await beginRecoveryEnroll("rs_1");
      expect(out.challenge_id).toBe("ch_1");
      expect(calls[0]!.url).toBe("/api/v1/recover/passkey/start");
      expect(calls[0]!.init.method).toBe("POST");
      const body = JSON.parse(calls[0]!.init.body as string) as Record<string, unknown>;
      expect(body).toEqual({ recovery_session_id: "rs_1" });
    } finally {
      restore();
    }
  });

  it("POSTs the attestation to /finish and returns the destructive blast-radius envelope", async () => {
    const { calls, restore } = installFetch([
      {
        status: 200,
        body: {
          user_id: "01HZ_USER",
          credential_id: "cred_new",
          revoked_credential_count: 1,
          revoked_session_count: 2,
        },
      },
    ]);
    try {
      const out = await finishRecoveryEnroll("rs_1", "ch_1", {
        id: "cid",
        rawId: "AQID",
        type: "public-key",
        response: { clientDataJSON: "AQ", attestationObject: "Ag" },
      });
      expect(out.user_id).toBe("01HZ_USER");
      expect(out.revoked_credential_count).toBe(1);
      expect(out.revoked_session_count).toBe(2);
      expect(calls[0]!.url).toBe("/api/v1/recover/passkey/finish");
      const body = JSON.parse(calls[0]!.init.body as string) as Record<string, unknown>;
      expect(body.recovery_session_id).toBe("rs_1");
      expect(body.challenge_id).toBe("ch_1");
      expect(body.credential).toBeDefined();
    } finally {
      restore();
    }
  });
});

// ── runRecoveryEnrollCeremony ─────────────────────────────────────

describe("runRecoveryEnrollCeremony — success path", () => {
  it("calls /start → navigator.credentials.create → /finish in order", async () => {
    const startBody = {
      challenge_id: "ch_1",
      options: {
        challenge: "AQID",
        rp: { name: "crew.day" },
        user: { id: "AQID", name: "u", displayName: "U" },
        pubKeyCredParams: [{ type: "public-key", alg: -7 }],
      },
    };
    const finishBody = {
      user_id: "01HZ_USER",
      credential_id: "cred_new",
      revoked_credential_count: 0,
      revoked_session_count: 1,
    };
    const { calls, restore } = installFetch([
      { status: 200, body: startBody },
      { status: 200, body: finishBody },
    ]);

    const fakeCredential = {
      id: "cred_new",
      rawId: bytes(0xaa, 0xbb),
      type: "public-key",
      response: {
        clientDataJSON: bytes(0x02),
        attestationObject: bytes(0x03),
        getTransports: () => ["internal"],
      },
      authenticatorAttachment: "platform" as const,
      getClientExtensionResults: () => ({}),
    } as unknown as PublicKeyCredential;
    const createSpy = vi.fn(async () => fakeCredential as unknown as Credential);
    Object.defineProperty(navigator, "credentials", {
      value: { create: createSpy },
      configurable: true,
    });

    try {
      const result = await runRecoveryEnrollCeremony("rs_1");
      expect(result.user_id).toBe("01HZ_USER");
      expect(result.revoked_session_count).toBe(1);

      // /start posted before navigator.create.
      expect(calls[0]!.url).toBe("/api/v1/recover/passkey/start");
      expect(createSpy).toHaveBeenCalledTimes(1);
      // /finish posted after navigator.create resolved.
      expect(calls[1]!.url).toBe("/api/v1/recover/passkey/finish");
      const finishPosted = JSON.parse(calls[1]!.init.body as string) as {
        recovery_session_id: string;
        challenge_id: string;
        credential: { rawId: string };
      };
      expect(finishPosted.recovery_session_id).toBe("rs_1");
      expect(finishPosted.challenge_id).toBe("ch_1");
      // The credential carried through is the one navigator.create
      // returned — base64url-encoded.
      expect(finishPosted.credential.rawId).toBe(bufToB64Url(bytes(0xaa, 0xbb)));
    } finally {
      restore();
      Object.defineProperty(navigator, "credentials", {
        value: undefined,
        configurable: true,
      });
    }
  });
});

describe("runRecoveryEnrollCeremony — error mapping (mapNavigatorError parity)", () => {
  /**
   * Drive `runRecoveryEnrollCeremony` to the point where
   * `navigator.credentials.create()` throws `domException`, then
   * return the rejection so the caller can assert on the typed
   * error class. Folds the `installFetch` + `defineProperty` boilerplate
   * so the per-DOMException assertions read like a mapping table —
   * mirrors `passkey.test.ts`'s `mapDomException` helper.
   */
  async function mapDomException(domException: DOMException): Promise<unknown> {
    const { restore } = installFetch([
      {
        status: 200,
        body: {
          challenge_id: "ch_1",
          options: {
            challenge: "AQID",
            rp: { name: "crew.day" },
            user: { id: "AQID", name: "u", displayName: "U" },
            pubKeyCredParams: [{ type: "public-key", alg: -7 }],
          },
        },
      },
    ]);
    const fakeCreds = {
      create: vi.fn(async () => {
        throw domException;
      }),
    };
    Object.defineProperty(navigator, "credentials", {
      value: fakeCreds,
      configurable: true,
    });
    try {
      try {
        await runRecoveryEnrollCeremony("rs_1");
        throw new Error("expected runRecoveryEnrollCeremony to reject");
      } catch (err) {
        return err;
      }
    } finally {
      restore();
      Object.defineProperty(navigator, "credentials", {
        value: undefined,
        configurable: true,
      });
    }
  }

  it("throws PasskeyUnsupportedError(platform_unsupported) when navigator.credentials is missing", async () => {
    const orig = navigator.credentials;
    Object.defineProperty(navigator, "credentials", {
      value: undefined,
      configurable: true,
    });
    try {
      const err = await runRecoveryEnrollCeremony("rs_1").catch((e: unknown) => e);
      expect(err).toBeInstanceOf(PasskeyUnsupportedError);
      expect((err as PasskeyUnsupportedError).kind).toBe("platform_unsupported");
    } finally {
      Object.defineProperty(navigator, "credentials", {
        value: orig,
        configurable: true,
      });
    }
  });

  it("maps NotAllowedError → PasskeyCancelledError", async () => {
    const err = await mapDomException(new DOMException("user cancelled", "NotAllowedError"));
    expect(err).toBeInstanceOf(PasskeyCancelledError);
  });

  it("maps AbortError → PasskeyCancelledError", async () => {
    const err = await mapDomException(new DOMException("aborted", "AbortError"));
    expect(err).toBeInstanceOf(PasskeyCancelledError);
  });

  it("maps TimeoutError → PasskeyTimeoutError", async () => {
    const err = await mapDomException(new DOMException("ceremony timed out", "TimeoutError"));
    expect(err).toBeInstanceOf(PasskeyTimeoutError);
  });

  it("maps UnknownError → PasskeyTransientError", async () => {
    const err = await mapDomException(new DOMException("authenticator hiccup", "UnknownError"));
    expect(err).toBeInstanceOf(PasskeyTransientError);
  });

  it("maps NetworkError → PasskeyTransientError", async () => {
    const err = await mapDomException(new DOMException("transport blew up", "NetworkError"));
    expect(err).toBeInstanceOf(PasskeyTransientError);
  });

  it("maps SecurityError → PasskeyUnsupportedError(kind='security')", async () => {
    const err = await mapDomException(new DOMException("insecure context", "SecurityError"));
    expect(err).toBeInstanceOf(PasskeyUnsupportedError);
    expect((err as PasskeyUnsupportedError).kind).toBe("security");
  });

  it("maps NotSupportedError → PasskeyUnsupportedError(kind='platform_unsupported')", async () => {
    const err = await mapDomException(new DOMException("alg not supported", "NotSupportedError"));
    expect(err).toBeInstanceOf(PasskeyUnsupportedError);
    expect((err as PasskeyUnsupportedError).kind).toBe("platform_unsupported");
  });

  it("maps InvalidStateError → PasskeyUnsupportedError(kind='invalid_state')", async () => {
    const err = await mapDomException(new DOMException("already enrolled", "InvalidStateError"));
    expect(err).toBeInstanceOf(PasskeyUnsupportedError);
    expect((err as PasskeyUnsupportedError).kind).toBe("invalid_state");
  });

  it("maps ConstraintError → PasskeyUnsupportedError(kind='constraint')", async () => {
    const err = await mapDomException(new DOMException("constraint not satisfied", "ConstraintError"));
    expect(err).toBeInstanceOf(PasskeyUnsupportedError);
    expect((err as PasskeyUnsupportedError).kind).toBe("constraint");
  });

  it("falls through to a generic Error for unrecognised DOMException names", async () => {
    const err = await mapDomException(new DOMException("???", "EncodingError"));
    expect(err).toBeInstanceOf(Error);
    expect(err).not.toBeInstanceOf(PasskeyCancelledError);
    expect(err).not.toBeInstanceOf(PasskeyTimeoutError);
    expect(err).not.toBeInstanceOf(PasskeyTransientError);
    expect(err).not.toBeInstanceOf(PasskeyUnsupportedError);
  });

  it("throws PasskeyUnsupportedError(platform_unsupported) when navigator returns a non-PublicKeyCredential", async () => {
    const { restore } = installFetch([
      {
        status: 200,
        body: {
          challenge_id: "ch_1",
          options: {
            challenge: "AQID",
            rp: { name: "crew.day" },
            user: { id: "AQID", name: "u", displayName: "U" },
            pubKeyCredParams: [{ type: "public-key", alg: -7 }],
          },
        },
      },
    ]);
    Object.defineProperty(navigator, "credentials", {
      // Resolve `null` — the spec allows it; a sane SPA must reject.
      value: { create: vi.fn(async () => null) },
      configurable: true,
    });
    try {
      const err = await runRecoveryEnrollCeremony("rs_1").catch((e: unknown) => e);
      expect(err).toBeInstanceOf(PasskeyUnsupportedError);
      expect((err as PasskeyUnsupportedError).kind).toBe("platform_unsupported");
    } finally {
      restore();
      Object.defineProperty(navigator, "credentials", {
        value: undefined,
        configurable: true,
      });
    }
  });
});
