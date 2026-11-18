/**
 * The registry's owner column is resolved in the browser from the user directory, with no
 * API field added. The decision under test: a known id gives its name, and an id the directory does
 * not return gives `null` -- never the raw id, never something inherited from `Object.prototype`.
 */
import { describe, expect, it } from "vitest";

import { ownerNameFor, ownerNamesFrom, UNRESOLVED_OWNER_LABEL } from "./ownerNames";

const DIRECTORY = [
  { id: "u-1", name: "김민지" },
  { id: "u-2", name: "박서준" },
];

describe("owner name resolution", () => {
  it("resolves a known owner id to the directory's name", () => {
    const names = ownerNamesFrom(DIRECTORY);

    expect(ownerNameFor(names, "u-1")).toBe("김민지");
    expect(ownerNameFor(names, "u-2")).toBe("박서준");
  });

  it("returns null for an owner the directory does not hold, not the id", () => {
    const names = ownerNamesFrom(DIRECTORY);

    expect(ownerNameFor(names, "u-gone")).toBeNull();
  });

  it("resolves nothing from an empty directory (the read failed or returned nothing)", () => {
    expect(ownerNameFor(ownerNamesFrom([]), "u-1")).toBeNull();
  });

  it("does not resolve an id that names an inherited property", () => {
    const names = ownerNamesFrom(DIRECTORY);

    expect(ownerNameFor(names, "constructor")).toBeNull();
    expect(ownerNameFor(names, "__proto__")).toBeNull();
    expect(ownerNameFor(names, "toString")).toBeNull();
  });

  it("skips an entry with a blank name instead of showing an empty owner", () => {
    const names = ownerNamesFrom([{ id: "u-3", name: "   " }]);

    expect(ownerNameFor(names, "u-3")).toBeNull();
  });

  it("words the unresolved case in Korean, apart from any id", () => {
    expect(UNRESOLVED_OWNER_LABEL).toBe("확인할 수 없음");
  });
});

describe("owner name display safety", () => {
  it("shows an invisible direction-changing character in an owner's name as a marker", () => {
    // Written as an escape on purpose: a raw bidi character in a source file is itself the hazard.
    const names = ownerNamesFrom([{ id: "u-4", name: "report\u202Etxt" }]);

    expect(ownerNameFor(names, "u-4")).toBe("report⟨U+202E⟩txt");
  });
});
