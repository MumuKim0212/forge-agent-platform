// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment 2026-08-30. Scope is exactly the ADR's:
 * keyboard operability, focus, and ARIA on `TemplatesPanel`'s row — not
 * rendering, not copy, not layout. `templatesApi` is the only import this
 * component makes beyond React (no context, no router), so mounting it
 * needs one api mock and nothing else.
 *
 * `ApprovalConsolePage`'s row carries the same wiring but is out of scope
 * (its row is nested inside a non-exported component and
 * mounting the page needs an identity provider plus three stubbed api
 * calls to reach one `tabIndex` — `TemplatesPanel` is the cheap subject the
 * ADR's named behaviours actually live on.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { templatesApi } from "../../api/templates";
import type { Template } from "../../types/api";
import { TemplatesPanel } from "./TemplatesPanel";

vi.mock("../../api/templates", () => ({
  templatesApi: {
    list: vi.fn(),
    get: vi.fn(),
  },
}));

const listMock = vi.mocked(templatesApi.list);
const getMock = vi.mocked(templatesApi.get);

const PORTAL_TEMPLATE: Template = {
  id: "tpl-portal",
  name: "포탈 템플릿",
  stack: "React + FastAPI",
  dockerfile_ref: "docker/portal.Dockerfile",
  deploy_config_ref: "deploy/portal.yaml",
  policy_profile: "default",
  version: "1.0.0",
  enabled: true,
};

async function renderRow() {
  listMock.mockResolvedValue([PORTAL_TEMPLATE]);
  getMock.mockResolvedValue(PORTAL_TEMPLATE);
  render(<TemplatesPanel />);
  return screen.findByRole("row", { name: "포탈 템플릿 상세 보기" });
}

describe("TemplatesPanel row — keyboard operability, focus, ARIA", () => {
  beforeEach(() => {
    listMock.mockReset();
    getMock.mockReset();
  });

  it("carries tabIndex 0 on the rendered row", async () => {
    const row = await renderRow();
    expect(row.tabIndex).toBe(0);
  });

  it("carries an aria-label naming the row (row has no accessible name from its content by itself)", async () => {
    const row = await renderRow();
    expect(row).toHaveAttribute("aria-label", "포탈 템플릿 상세 보기");
  });

  it("aria-current is false before activation and tracks selection to true after Enter", async () => {
    const row = await renderRow();
    expect(row).toHaveAttribute("aria-current", "false");

    fireEvent.keyDown(row, { key: "Enter" });
    await waitFor(() => expect(row).toHaveAttribute("aria-current", "true"));
  });

  it("Enter activates the row", async () => {
    const row = await renderRow();
    fireEvent.keyDown(row, { key: "Enter" });

    // `inspect(id)` calls `templatesApi.get` synchronously, before its own
    // first `await` — no waitFor needed to observe the call itself.
    expect(getMock).toHaveBeenCalledTimes(1);
    expect(getMock).toHaveBeenCalledWith("tpl-portal");
  });

  it("Space does not activate the row", async () => {
    const row = await renderRow();
    fireEvent.keyDown(row, { key: " " });

    expect(getMock).not.toHaveBeenCalled();
    expect(row).toHaveAttribute("aria-current", "false");
  });

  it("a keydown bubbling up from a nested cell does not activate the row (the regression shouldActivateRow guards against)", async () => {
    const row = await renderRow();
    const cell = row.querySelector("td");
    if (!cell) {
      throw new Error("expected the row to render at least one cell");
    }

    fireEvent.keyDown(cell, { key: "Enter" });

    expect(getMock).not.toHaveBeenCalled();
  });
});
