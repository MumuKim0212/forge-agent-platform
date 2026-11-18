/**
 * the `/openapi.json` URL follows the configured API base. The base is a build-time constant
 * in `client.ts`, so the shapes are tested on the pure function; the wiring (the configured base
 * reaches the probe) is exercised through `IdentityContext.test.ts`.
 */
import { describe, expect, it } from "vitest";

import { openApiUrlForBase } from "./client";

describe("openApiUrlForBase", () => {
  it("the same-origin base /api gives /openapi.json", () => {
    expect(openApiUrlForBase("/api")).toBe("/openapi.json");
  });

  it("an absolute base gives that origin's /openapi.json", () => {
    expect(openApiUrlForBase("http://localhost:8000/api")).toBe("http://localhost:8000/openapi.json");
    expect(openApiUrlForBase("https://forge.example.com/api")).toBe(
      "https://forge.example.com/openapi.json",
    );
  });

  it("a trailing slash on the base does not change the answer", () => {
    expect(openApiUrlForBase("/api/")).toBe("/openapi.json");
    expect(openApiUrlForBase("http://localhost:8000/api/")).toBe("http://localhost:8000/openapi.json");
  });

  it("a base without the /api suffix is taken as the API root", () => {
    expect(openApiUrlForBase("http://localhost:8000")).toBe("http://localhost:8000/openapi.json");
  });
});
