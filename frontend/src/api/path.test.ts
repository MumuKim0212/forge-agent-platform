/**
 * `pathSegment` is the one place an id becomes part of a request path. The expected values
 * are literals: re-deriving them with `encodeURIComponent` here would make
 * the test agree with whatever the helper does.
 */
import { describe, expect, it } from "vitest";

import { pathSegment } from "./path";

describe("pathSegment", () => {
  it("leaves an ordinary id (a UUID) alone", () => {
    expect(pathSegment("0b6f1c2e-8a4d-4f3b-9c1e-5d7a2b3c4d5e")).toBe("0b6f1c2e-8a4d-4f3b-9c1e-5d7a2b3c4d5e");
  });

  it("escapes a slash, so the id cannot end its segment", () => {
    expect(pathSegment("a/b")).toBe("a%2Fb");
  });

  it("escapes a question mark, so the id cannot start a query string", () => {
    expect(pathSegment("a?x=1")).toBe("a%3Fx%3D1");
  });

  it("escapes a hash, so the id cannot start a fragment", () => {
    expect(pathSegment("a#b")).toBe("a%23b");
  });

  it("escapes a percent sign, so an id that already looks encoded is not decoded on the far side", () => {
    expect(pathSegment("a%2Fb")).toBe("a%252Fb");
  });

  it("escapes the slashes of a traversal id; the dots that remain cannot walk the path", () => {
    expect(pathSegment("../admin")).toBe("..%2Fadmin");
    expect(pathSegment("a/../b")).toBe("a%2F..%2Fb");
  });

  it("does not let a %2e spelling of a dot segment through", () => {
    // `%2e%2e` is a dot segment to a URL parser; as an id it is escaped to something that is not.
    expect(pathSegment("%2e%2e")).toBe("%252e%252e");
  });

  it("escapes a space and a non-ASCII id", () => {
    expect(pathSegment("a b")).toBe("a%20b");
    expect(pathSegment("가")).toBe("%EA%B0%80");
  });

  it("refuses the dot segments, which no encoding can protect", () => {
    expect(() => pathSegment("..")).toThrow(RangeError);
    expect(() => pathSegment(".")).toThrow(RangeError);
  });

  it("refuses an empty id: the empty segment is a different route, not a missing project", () => {
    expect(() => pathSegment("")).toThrow(RangeError);
  });

  it("accepts an id that merely contains or starts with dots", () => {
    expect(pathSegment("...")).toBe("...");
    expect(pathSegment(".hidden")).toBe(".hidden");
    expect(pathSegment("a..b")).toBe("a..b");
  });
});
