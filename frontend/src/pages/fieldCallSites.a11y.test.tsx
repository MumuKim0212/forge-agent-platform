// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment 2026-08-30: the call sites of `Field` whose announcement was
 * decided, mounted for real so a call site cannot drift from the decision. Scope is ARIA only: which
 * regions are live regions and what each control is described by, never the copy.
 *
 * - The three length-limited fields (`RequestIntakePage`'s name and request, `ProjectDetailPage`'s
 *   change request): typing past the limit is caused by the user's own keystrokes, so it must create
 *   no alert, and the polite status region that says so must stay untouched while they keep typing.
 * - The signup form: its field errors answer a submit, so each stays an assertive alert, and each is
 *   the description of its own control.
 *
 * The api modules and the identity hook are mocked; the routes are a memory router.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { deploymentsApi } from "../api/deployments";
import { projectsApi } from "../api/projects";
import { teamsApi, templatesApi } from "../api/templates";
import type { ProjectDetail, Team, Template } from "../types/api";
import { ProjectDetailPage } from "./ProjectDetailPage";
import { RequestIntakePage } from "./RequestIntakePage";
import { SignupRequestPage } from "./SignupRequestPage";

vi.mock("../api/templates", () => ({ templatesApi: { list: vi.fn() }, teamsApi: { list: vi.fn() } }));
vi.mock("../api/projects", () => ({
  projectsApi: { create: vi.fn(), get: vi.fn(), timeline: vi.fn(), submitChangeRequest: vi.fn() },
}));
vi.mock("../api/deployments", () => ({
  deploymentsApi: { list: vi.fn(), confirm: vi.fn(), rollback: vi.fn(), stop: vi.fn() },
}));
vi.mock("../context/IdentityContext", () => ({
  useIdentity: () => ({
    currentUser: { id: "u1", name: "소유자", role: "requester" },
    mode: "local",
    loading: false,
  }),
}));

/** Opts in to the router's v7 behaviour, which only silences its console warning here. */
const FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true };

const PROJECT = {
  id: "p1",
  name: "휴가 도구",
  owner_id: "u1",
  team_id: "t1",
  template_id: "tpl1",
  intent_spec: null,
  status: "ACTIVE",
  repo_url: null,
  container_id: null,
  confirmed_ref: "c0ffee0", // the change-request form needs a confirmed ref
  created_at: "2026-10-01T00:00:00Z",
  last_active_at: "2026-10-01T00:00:00Z",
  owner_name: "소유자",
  team_name: "팀",
  template_name: "템플릿",
} as ProjectDetail;

beforeEach(() => {
  vi.mocked(templatesApi.list).mockReset().mockResolvedValue([{ id: "tpl1", name: "웹 앱" }] as Template[]);
  vi.mocked(teamsApi.list).mockReset().mockResolvedValue([{ id: "t1", name: "플랫폼팀" }] as Team[]);
  vi.mocked(projectsApi.get).mockReset().mockResolvedValue(PROJECT);
  vi.mocked(projectsApi.timeline).mockReset().mockResolvedValue([]);
  vi.mocked(deploymentsApi.list).mockReset().mockResolvedValue([]);
});

/** Past the limit and still typing: no alert at any step, one stable polite notice, untouched. */
type User = ReturnType<typeof userEvent.setup>;

async function expectQuietWhileTypingPastLimit(control: HTMLElement, atLimit: (user: User) => Promise<void>) {
  const user = userEvent.setup();
  const field = control.closest("div") as HTMLElement;
  const status = within(field).getByRole("status");
  expect(status).toBeEmptyDOMElement();

  await user.click(control);
  await atLimit(user);
  expect(screen.queryAllByRole("alert")).toHaveLength(0);
  expect(status).toBeEmptyDOMElement();

  await user.keyboard("가");
  expect(screen.queryAllByRole("alert")).toHaveLength(0);
  const announced = status.textContent;
  expect(announced).toBeTruthy();
  const described = control.getAttribute("aria-describedby") as string;
  const countBefore = document.getElementById(described)?.textContent;

  const observer = new MutationObserver(() => {});
  observer.observe(status, { childList: true, characterData: true, subtree: true, attributes: true });
  await user.keyboard("나다라");

  expect(screen.queryAllByRole("alert")).toHaveLength(0);
  expect(observer.takeRecords()).toHaveLength(0);
  observer.disconnect();
  expect(status.textContent).toBe(announced);
  expect(document.getElementById(described)?.textContent).not.toBe(countBefore);
}

describe("the length-limited fields stay quiet while the user types past the limit", () => {
  it("RequestIntakePage: the project name", async () => {
    render(
      <MemoryRouter future={FUTURE}>
        <RequestIntakePage />
      </MemoryRouter>,
    );
    const control = await screen.findByLabelText(/프로젝트 이름/);
    expect(control).toHaveAccessibleDescription("0 / 100");

    await expectQuietWhileTypingPastLimit(control, async (user) => {
      await user.paste("가".repeat(100));
    });
  });

  it("RequestIntakePage: the request text", async () => {
    render(
      <MemoryRouter future={FUTURE}>
        <RequestIntakePage />
      </MemoryRouter>,
    );
    const control = await screen.findByLabelText(/요청 내용/);
    expect(control).toHaveAccessibleDescription("0 / 8000");

    await expectQuietWhileTypingPastLimit(control, async (user) => {
      await user.paste("가".repeat(8000));
    });
  });

  it("ProjectDetailPage: the change request", async () => {
    render(
      <MemoryRouter initialEntries={["/projects/p1"]} future={FUTURE}>
        <Routes>
          <Route path="/projects/:id" element={<ProjectDetailPage />} />
        </Routes>
      </MemoryRouter>,
    );
    // The form is on the 변경 요청 tab, not the one the page opens on. The panel is labelled
    // by its tab, also named 변경 요청, so the field is looked up by role, not by label text.
    await userEvent.setup().click(await screen.findByRole("tab", { name: "변경 요청" }));
    const control = await screen.findByRole("textbox", { name: "변경 요청" });
    expect(control).toHaveAccessibleDescription("0 / 8000");

    await expectQuietWhileTypingPastLimit(control, async (user) => {
      await user.paste("가".repeat(8000));
    });
  });
});

describe("the signup form's field errors answer a submit, so each is an alert and its control's description", () => {
  it("shows four alerts after an empty submit, each describing the control it belongs to", async () => {
    render(
      <MemoryRouter future={FUTURE}>
        <SignupRequestPage />
      </MemoryRouter>,
    );
    fireEvent.submit(document.querySelector("form") as HTMLFormElement);

    const alerts = await screen.findAllByRole("alert");
    expect(alerts).toHaveLength(4);
    for (const id of ["signup-email", "signup-name", "signup-department", "signup-password"]) {
      const control = document.getElementById(id) as HTMLElement;
      const described = control.getAttribute("aria-describedby") as string;
      expect(alerts.map((alert) => alert.id), id).toContain(described);
    }
  });
});
