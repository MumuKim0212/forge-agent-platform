// @vitest-environment jsdom
/**
 * The console has no header paragraph and no 관리자 전용 badge row, so
 * the page opens on the tabs (the shell's h1 and the admin-only nav entry say where the person is). The panels are stubbed (each has its own loader and its own tests); the identity hook
 * supplies an admin in `dev` mode, so the page opens on the usage tab.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AdminConsolePage } from "./AdminConsolePage";

vi.mock("./admin/AgentEnginePanel", () => ({ AgentEnginePanel: () => null }));
vi.mock("./admin/ConfigurationPanel", () => ({ ConfigurationPanel: () => null }));
vi.mock("./admin/DeploymentHealthPanel", () => ({ DeploymentHealthPanel: () => null }));
vi.mock("./admin/EvalRunsPanel", () => ({ EvalRunsPanel: () => null }));
vi.mock("./admin/HostHealthPanel", () => ({ HostHealthPanel: () => null }));
vi.mock("./admin/RolesPanel", () => ({ RolesPanel: () => null }));
vi.mock("./admin/SignupRequestsPanel", () => ({ SignupRequestsPanel: () => null }));
vi.mock("./admin/TemplatesPanel", () => ({ TemplatesPanel: () => null }));
vi.mock("./admin/UsagePanel", () => ({ UsagePanel: () => null }));
vi.mock("../context/IdentityContext", () => ({
  useIdentity: () => ({
    mode: "dev",
    currentUser: { id: "u-admin", name: "관리자", email: "a@example.com", role: "admin", team_id: null },
  }),
}));

describe("AdminConsolePage header", () => {
  it("shows no 관리자 전용 badge and no explanatory paragraph, and the tabs still render", () => {
    const { container } = render(<AdminConsolePage />);

    expect(screen.queryByText("관리자 전용")).toBeNull();
    expect(container.textContent).not.toContain("한 곳에서 봅니다");
    expect(container.textContent).not.toContain("조회 전용입니다");
    // No header row of its own: the tab list is the first thing in the page.
    expect(container.firstElementChild?.firstElementChild).toBe(screen.getByRole("tablist"));
    expect(screen.getAllByRole("tab").length).toBeGreaterThan(0);
    expect(screen.getByRole("tabpanel")).toBeInTheDocument();
  });
});
