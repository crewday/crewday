import { describe, expect, it } from "vitest";

import { installFetchRouteHandlers } from "./helpers";

describe("installFetchRouteHandlers", () => {
  it("captures Request input method, body, headers, and query string", async () => {
    const fetcher = installFetchRouteHandlers([
      {
        path: "/api/v1/things?limit=1",
        method: "POST",
        respond: (request) => ({
          status: 201,
          body: {
            body: request.body,
            contentType: request.headers["content-type"],
            method: request.method,
          },
        }),
      },
    ]);

    const response = await fetch(
      new Request("http://crewday.test/api/v1/things?limit=1", {
        body: JSON.stringify({ name: " mop " }),
        headers: new Headers({ "Content-Type": "application/json" }),
        method: "post",
      }),
    );

    expect(response.status).toBe(201);
    await expect(response.text()).resolves.toBe(
      JSON.stringify({
        body: { name: " mop " },
        contentType: "application/json",
        method: "POST",
      }),
    );
    expect(fetcher.requests).toHaveLength(1);
    expect(fetcher.requests[0]).toMatchObject({
      path: "/api/v1/things?limit=1",
      method: "POST",
      body: { name: " mop " },
      headers: { "content-type": "application/json" },
    });
  });

  it("preserves non-JSON string bodies for request assertions", async () => {
    const fetcher = installFetchRouteHandlers([
      {
        path: "/api/v1/ping",
        method: "POST",
        respond: (request) => ({ body: { body: request.body } }),
      },
    ]);

    const response = await fetch("/api/v1/ping", {
      body: "plain ping",
      method: "POST",
    });

    await expect(response.text()).resolves.toBe(
      JSON.stringify({ body: "plain ping" }),
    );
    expect(fetcher.requests[0]?.body).toBe("plain ping");
  });
});
