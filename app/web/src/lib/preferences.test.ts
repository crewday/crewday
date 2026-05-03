import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  initialAgentCollapsed,
  initialNavCollapsed,
  persistAgentCollapsed,
  persistNavCollapsed,
  persistRole,
  persistTheme,
  persistWorkspace,
  readAgentCollapsedCookie,
  readNavCollapsedCookie,
  readRoleCookie,
  readThemeCookie,
  readWorkspaceCookie,
} from "@/lib/preferences";

// Hermetic cookie helpers — jsdom persists ``document.cookie`` between
// tests, so each case explicitly sets and clears just the cookie it
// cares about. Setting ``max-age=0`` is the canonical "drop this
// cookie now" idiom that survives both the in-memory jsdom store and
// the browser-style mirror tests would otherwise see.
const COOKIES = [
  "crewday_role",
  "crewday_theme",
  "crewday_workspace",
  "crewday_agent_collapsed",
  "crewday_nav_collapsed",
];

function clearCookies(): void {
  for (const name of COOKIES) {
    document.cookie = `${name}=; path=/; max-age=0`;
  }
}

function setCookie(name: string, value: string): void {
  document.cookie = `${name}=${value}; path=/`;
}

beforeEach(() => {
  clearCookies();
});

afterEach(() => {
  clearCookies();
});

describe("readRoleCookie", () => {
  it("defaults to 'employee' when the cookie is missing", () => {
    expect(readRoleCookie()).toBe("employee");
  });

  it("returns 'manager' when crewday_role=manager", () => {
    setCookie("crewday_role", "manager");
    expect(readRoleCookie()).toBe("manager");
  });

  it("returns 'client' when crewday_role=client", () => {
    setCookie("crewday_role", "client");
    expect(readRoleCookie()).toBe("client");
  });

  it("returns 'admin' when crewday_role=admin", () => {
    setCookie("crewday_role", "admin");
    expect(readRoleCookie()).toBe("admin");
  });

  it("falls back to 'employee' for unknown values", () => {
    setCookie("crewday_role", "wat");
    expect(readRoleCookie()).toBe("employee");
  });
});

describe("readWorkspaceCookie", () => {
  it("returns null when the cookie is missing", () => {
    expect(readWorkspaceCookie()).toBeNull();
  });

  it("returns the cookie value verbatim", () => {
    setCookie("crewday_workspace", "01HW7XYZ");
    expect(readWorkspaceCookie()).toBe("01HW7XYZ");
  });
});

describe("readThemeCookie", () => {
  it("defaults to 'system' when the cookie is missing", () => {
    expect(readThemeCookie()).toBe("system");
  });

  it("returns 'dark' / 'light' / 'system' verbatim", () => {
    setCookie("crewday_theme", "dark");
    expect(readThemeCookie()).toBe("dark");
    setCookie("crewday_theme", "light");
    expect(readThemeCookie()).toBe("light");
    setCookie("crewday_theme", "system");
    expect(readThemeCookie()).toBe("system");
  });

  it("falls back to 'system' for unknown values", () => {
    setCookie("crewday_theme", "neon");
    expect(readThemeCookie()).toBe("system");
  });
});

describe("readAgentCollapsedCookie", () => {
  it("returns null when the cookie is missing (tri-state)", () => {
    expect(readAgentCollapsedCookie()).toBeNull();
  });

  it("returns true when the cookie is '1'", () => {
    setCookie("crewday_agent_collapsed", "1");
    expect(readAgentCollapsedCookie()).toBe(true);
  });

  it("returns false when the cookie is '0'", () => {
    setCookie("crewday_agent_collapsed", "0");
    expect(readAgentCollapsedCookie()).toBe(false);
  });

  it("returns null for any other value", () => {
    setCookie("crewday_agent_collapsed", "wat");
    expect(readAgentCollapsedCookie()).toBeNull();
  });
});

describe("readNavCollapsedCookie", () => {
  it("returns null when the cookie is missing", () => {
    expect(readNavCollapsedCookie()).toBeNull();
  });

  it("returns the tri-state for '1' / '0' / unknown", () => {
    setCookie("crewday_nav_collapsed", "1");
    expect(readNavCollapsedCookie()).toBe(true);
    setCookie("crewday_nav_collapsed", "0");
    expect(readNavCollapsedCookie()).toBe(false);
    setCookie("crewday_nav_collapsed", "wat");
    expect(readNavCollapsedCookie()).toBeNull();
  });
});

describe("initialAgentCollapsed", () => {
  it("honours the cookie when set", () => {
    setCookie("crewday_agent_collapsed", "1");
    expect(initialAgentCollapsed()).toBe(true);
    setCookie("crewday_agent_collapsed", "0");
    expect(initialAgentCollapsed()).toBe(false);
  });

  it("falls back to a viewport-driven default when the cookie is missing", () => {
    // jsdom's default innerWidth is 1024 — well below the 1600 wide-
    // desktop threshold — so the rail starts collapsed by default.
    expect(initialAgentCollapsed()).toBe(true);
  });
});

describe("initialNavCollapsed", () => {
  it("honours the cookie when set", () => {
    setCookie("crewday_nav_collapsed", "1");
    expect(initialNavCollapsed()).toBe(true);
    setCookie("crewday_nav_collapsed", "0");
    expect(initialNavCollapsed()).toBe(false);
  });

  it("falls back to a viewport-driven default when the cookie is missing", () => {
    // jsdom's default innerWidth is 1024 — below the 1200 nav threshold
    // — so the nav starts collapsed.
    expect(initialNavCollapsed()).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Writers — stub fetch / sendBeacon and assert the URL + method.
// ---------------------------------------------------------------------------

interface FetchCall {
  url: string;
  init: RequestInit;
}

function installFetch(): { calls: FetchCall[]; restore: () => void } {
  const calls: FetchCall[] = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    return {
      ok: true,
      status: 204,
      statusText: "No Content",
      text: async () => "",
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

function installSendBeacon(returnValue: boolean): {
  calls: Array<{ url: string; data: BodyInit | null | undefined }>;
  restore: () => void;
} {
  const calls: Array<{ url: string; data: BodyInit | null | undefined }> = [];
  const original = (navigator as Navigator & { sendBeacon?: typeof navigator.sendBeacon })
    .sendBeacon;
  const spy = vi.fn((url: string | URL, data?: BodyInit | null) => {
    calls.push({ url: url.toString(), data });
    return returnValue;
  });
  (navigator as unknown as { sendBeacon: typeof navigator.sendBeacon }).sendBeacon =
    spy as unknown as typeof navigator.sendBeacon;
  return {
    calls,
    restore: () => {
      if (original === undefined) {
        delete (navigator as { sendBeacon?: typeof navigator.sendBeacon }).sendBeacon;
      } else {
        (navigator as unknown as { sendBeacon: typeof navigator.sendBeacon }).sendBeacon =
          original;
      }
    },
  };
}

describe("persistRole", () => {
  it("POSTs to /switch/<role>", () => {
    const fakeFetch = installFetch();
    try {
      persistRole("manager");
      expect(fakeFetch.calls).toHaveLength(1);
      expect(fakeFetch.calls[0]!.url).toBe("/switch/manager");
      expect(fakeFetch.calls[0]!.init.method).toBe("POST");
      expect(fakeFetch.calls[0]!.init.credentials).toBe("same-origin");
    } finally {
      fakeFetch.restore();
    }
  });
});

describe("persistTheme", () => {
  it("POSTs to /theme/set/<theme>", () => {
    const fakeFetch = installFetch();
    try {
      persistTheme("dark");
      expect(fakeFetch.calls).toHaveLength(1);
      expect(fakeFetch.calls[0]!.url).toBe("/theme/set/dark");
      expect(fakeFetch.calls[0]!.init.method).toBe("POST");
    } finally {
      fakeFetch.restore();
    }
  });
});

describe("persistWorkspace", () => {
  it("POSTs to /workspaces/switch/<wsid>", () => {
    const fakeFetch = installFetch();
    try {
      persistWorkspace("01HW7XYZ");
      expect(fakeFetch.calls).toHaveLength(1);
      expect(fakeFetch.calls[0]!.url).toBe("/workspaces/switch/01HW7XYZ");
      expect(fakeFetch.calls[0]!.init.method).toBe("POST");
    } finally {
      fakeFetch.restore();
    }
  });
});

describe("persistAgentCollapsed", () => {
  it("uses navigator.sendBeacon when available", () => {
    const fakeBeacon = installSendBeacon(true);
    const fakeFetch = installFetch();
    try {
      persistAgentCollapsed("collapsed");
      expect(fakeBeacon.calls).toHaveLength(1);
      expect(fakeBeacon.calls[0]!.url).toBe("/agent/sidebar/collapsed");
      // sendBeacon delivered → fetch is not called.
      expect(fakeFetch.calls).toHaveLength(0);
    } finally {
      fakeBeacon.restore();
      fakeFetch.restore();
    }
  });

  it("falls back to fetch when sendBeacon returns false", () => {
    const fakeBeacon = installSendBeacon(false);
    const fakeFetch = installFetch();
    try {
      persistAgentCollapsed("open");
      expect(fakeBeacon.calls).toHaveLength(1);
      expect(fakeFetch.calls).toHaveLength(1);
      expect(fakeFetch.calls[0]!.url).toBe("/agent/sidebar/open");
      expect(fakeFetch.calls[0]!.init.method).toBe("POST");
    } finally {
      fakeBeacon.restore();
      fakeFetch.restore();
    }
  });
});

describe("persistNavCollapsed", () => {
  it("uses navigator.sendBeacon when available", () => {
    const fakeBeacon = installSendBeacon(true);
    const fakeFetch = installFetch();
    try {
      persistNavCollapsed("collapsed");
      expect(fakeBeacon.calls).toHaveLength(1);
      expect(fakeBeacon.calls[0]!.url).toBe("/nav/sidebar/collapsed");
      expect(fakeFetch.calls).toHaveLength(0);
    } finally {
      fakeBeacon.restore();
      fakeFetch.restore();
    }
  });

  it("falls back to fetch when sendBeacon returns false", () => {
    const fakeBeacon = installSendBeacon(false);
    const fakeFetch = installFetch();
    try {
      persistNavCollapsed("open");
      expect(fakeFetch.calls).toHaveLength(1);
      expect(fakeFetch.calls[0]!.url).toBe("/nav/sidebar/open");
    } finally {
      fakeBeacon.restore();
      fakeFetch.restore();
    }
  });
});
