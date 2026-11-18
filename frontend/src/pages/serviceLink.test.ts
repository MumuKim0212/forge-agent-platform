/**
 * when the project page offers "서비스 열기" (ADR 0021: the decision, not the rendering).
 * Literals on the expected side; the backend's two real shapes are the first two rows.
 */
import { describe, expect, it } from "vitest";

import { OPEN_SERVICE_LABEL, serviceLinkHref } from "./serviceLink";

describe("serviceLinkHref", () => {
  const table: Array<[string, { service_url: string | null } | null, string | null]> = [
    ["prod: absolute Traefik URL", { service_url: "http://p1.apps.example.test" }, "http://p1.apps.example.test"],
    ["prod over TLS", { service_url: "https://p1.apps.example.test" }, "https://p1.apps.example.test"],
    ["dev: relative proxy route", { service_url: "/api/projects/p1/service/" }, "/api/projects/p1/service/"],
    ["no active deployment (null)", { service_url: null }, null],
    ["empty string", { service_url: "" }, null],
    ["no project", null, null],
    ["javascript: is never a link", { service_url: "javascript:alert(1)" }, null],
    ["data: is never a link", { service_url: "data:text/html,x" }, null],
    ["protocol-relative is not same-origin", { service_url: "//evil.example/x" }, null],
    ["a bare word is not a URL", { service_url: "p1.apps.example.test" }, null],
    ["scheme with nothing after it", { service_url: "http://" }, null],
  ];
  for (const [name, project, expected] of table) {
    it(name, () => {
      expect(serviceLinkHref(project)).toBe(expected);
    });
  }
});

describe("OPEN_SERVICE_LABEL", () => {
  it("is the pinned copy", () => {
    expect(OPEN_SERVICE_LABEL).toBe("서비스 열기");
  });
});
