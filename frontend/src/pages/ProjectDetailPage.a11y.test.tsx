// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment 2026-08-30. Scope is the ADR's: keyboard operability, focus and
 * ARIA on the project page's un-archive control and its in-page confirmation, not rendering and not
 * copy (the words are `unarchive.test.ts`'s). Opening the confirmation moves focus into it; leaving
 * it by cancelling, by Escape, or by an un-archive that ended (done or refused) must put focus back
 * on the control, or, when the outcome removed the control (the project is no longer archived), on
 * the status line that now shows the result, never drop it to the page (the defect fixed on the
 * eval start control: a disabled button cannot take focus).
 *
 * The other half of the same live region: a failed un-archive's alert (`role="alert"`,
 * assertive) must not outlive what it was about. It goes when another page action starts and when
 * the control stops being offered, except the refusal that made the control go (that one is the
 * explanation of why it is gone).
 *
 * The api modules and the identity hook are mocked; the route is a memory router. A test in this
 * file that sets focus by hand does so only to stand where a keyboard user would already be.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { deploymentsApi } from "../api/deployments";
import { projectsApi } from "../api/projects";
import type { DeployControls, ProjectDetail } from "../types/api";
import { ProjectDetailPage } from "./ProjectDetailPage";

vi.mock("../api/projects", () => ({
  projectsApi: { get: vi.fn(), timeline: vi.fn(), submitChangeRequest: vi.fn(), unarchive: vi.fn() },
}));
vi.mock("../api/deployments", () => ({
  deploymentsApi: { list: vi.fn(), confirm: vi.fn(), rollback: vi.fn(), stop: vi.fn() },
}));

const viewer = vi.hoisted(() => ({ current: { id: "u-owner", name: "소유자", role: "requester" } }));
vi.mock("../context/IdentityContext", () => ({
  useIdentity: () => ({ currentUser: viewer.current, mode: "local", loading: false }),
}));

const getMock = vi.mocked(projectsApi.get);
const unarchiveMock = vi.mocked(projectsApi.unarchive);

/** Opts in to the router's v7 behaviour, which only silences its console warning here. */
const FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true };

const BLOCKED: DeployControls = {
  confirmable_build_id: null,
  confirm_blocked_reason: "PROJECT_ARCHIVED",
  rollback_target_id: null,
  rollback_blocked_reason: "PROJECT_ARCHIVED",
  active_deployment_id: null,
  in_flight: false,
};
const OFFERED: DeployControls = {
  ...BLOCKED,
  confirmable_build_id: "bld-1",
  confirm_blocked_reason: null,
};

function project(status: string, controls: DeployControls): ProjectDetail {
  return {
    id: "p1",
    name: "휴가 도구",
    owner_id: "u-owner",
    team_id: "t1",
    template_id: "tpl1",
    intent_spec: null,
    status,
    repo_url: null,
    container_id: null,
    confirmed_ref: "c0ffee0", // a change request needs a confirmed ref
    created_at: "2026-10-01T00:00:00Z",
    last_active_at: "2026-10-01T00:00:00Z",
    owner_name: "소유자",
    team_name: "팀",
    template_name: "템플릿",
    service_url: null,
    deploy_controls: controls,
  };
}

const ARCHIVED = project("ARCHIVED", BLOCKED);
const RESTORED = project("ACTIVE", OFFERED);

beforeEach(() => {
  viewer.current = { id: "u-owner", name: "소유자", role: "requester" };
  getMock.mockReset().mockResolvedValue(ARCHIVED);
  vi.mocked(projectsApi.timeline).mockReset().mockResolvedValue([]);
  vi.mocked(deploymentsApi.list).mockReset().mockResolvedValue([]);
  unarchiveMock.mockReset();
});

function tree() {
  return (
    <MemoryRouter initialEntries={["/projects/p1"]} future={FUTURE}>
      <Routes>
        <Route path="/projects/:id" element={<ProjectDetailPage />} />
      </Routes>
    </MemoryRouter>
  );
}

function mount() {
  return render(tree());
}

const TRIGGER = { name: "보관 해제" } as const;
const CONFIRM_GROUP = { name: "보관을 해제할까요?" } as const;

/** The status tile (label `상태` plus the badge), the element that takes focus when the control is gone. */
function statusLine(): HTMLElement {
  return screen.getByText("상태").parentElement as HTMLElement;
}

/** The change-request field by its accessible name: the tab panel is labelled by its tab, which is
 * also named 변경 요청, so a label lookup finds two elements. */
const FORM_FIELD = { name: "변경 요청" } as const;

/** The form lives on the 변경 요청 tab, which is not the one the page opens on. */
async function openChangeRequestTab(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("tab", { name: "변경 요청" }));
}

async function openConfirmation(user: ReturnType<typeof userEvent.setup>) {
  const view = mount();
  const trigger = await screen.findByRole("button", TRIGGER);
  trigger.focus();
  await user.keyboard("{Enter}");
  const group = await screen.findByRole("group", CONFIRM_GROUP);
  return { trigger, group, view };
}

describe("who can reach the un-archive control by keyboard", () => {
  it("the owner of an archived project has it in the tab order", async () => {
    mount();

    const trigger = await screen.findByRole("button", TRIGGER);

    expect(trigger).toBeEnabled();
    expect(trigger.tabIndex).toBe(0);
  });

  it("another requester has no such control", async () => {
    viewer.current = { id: "u-other", name: "다른 사람", role: "requester" };
    mount();

    await screen.findByText("상태");

    expect(screen.queryByRole("button", TRIGGER)).toBeNull();
  });

  it("an admin who does not own it has it", async () => {
    viewer.current = { id: "u-admin", name: "관리자", role: "admin" };
    mount();

    expect(await screen.findByRole("button", TRIGGER)).toBeEnabled();
  });

  it("an approver who does not own it has none (an approver does not override ownership)", async () => {
    viewer.current = { id: "u-approver", name: "승인자", role: "approver" };
    mount();

    await screen.findByText("상태");

    expect(screen.queryByRole("button", TRIGGER)).toBeNull();
  });

  it("an owner whose project is not archived has none", async () => {
    getMock.mockResolvedValue(RESTORED);
    mount();

    await screen.findByText("상태");

    expect(screen.queryByRole("button", TRIGGER)).toBeNull();
  });
});

describe("the confirmation is a named group a keyboard can operate", () => {
  it("Enter on the control opens it and moves focus into it", async () => {
    const user = userEvent.setup();
    const { trigger, group } = await openConfirmation(user);

    await waitFor(() => expect(document.activeElement).toBe(group));
    expect(trigger).toBeDisabled();
  });

  it("Tab reaches the confirm button and then the cancel button, in that order", async () => {
    const user = userEvent.setup();
    const { group } = await openConfirmation(user);
    await waitFor(() => expect(document.activeElement).toBe(group));

    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "해제" }));

    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "취소" }));
  });
});

describe("focus after the confirmation is cancelled", () => {
  it("returns to the control after Enter on 취소", async () => {
    const user = userEvent.setup();
    const { trigger, group } = await openConfirmation(user);
    await waitFor(() => expect(document.activeElement).toBe(group));
    await user.tab();
    await user.tab();

    await user.keyboard("{Enter}");

    await waitFor(() => expect(screen.queryByRole("group", CONFIRM_GROUP)).toBeNull());
    expect(trigger).toBeEnabled();
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(unarchiveMock).not.toHaveBeenCalled();
  });

  it("returns to the control after Escape, from the group and from a button inside it", async () => {
    const user = userEvent.setup();
    const { trigger, group } = await openConfirmation(user);
    await waitFor(() => expect(document.activeElement).toBe(group));

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("group", CONFIRM_GROUP)).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));

    await user.keyboard("{Enter}");
    await screen.findByRole("group", CONFIRM_GROUP);
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "해제" }));

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("group", CONFIRM_GROUP)).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(unarchiveMock).not.toHaveBeenCalled();
  });

  it("while the request is in flight Escape does not close the box and 취소 is disabled", async () => {
    let finish: (value: ProjectDetail) => void = () => {};
    unarchiveMock.mockReturnValue(new Promise<ProjectDetail>((resolve) => (finish = resolve)));
    const user = userEvent.setup();
    const { group } = await openConfirmation(user);
    await waitFor(() => expect(document.activeElement).toBe(group));
    await user.tab();

    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.getByRole("button", { name: "취소" })).toBeDisabled());
    // The busy confirm button is disabled and drops focus, so a key typed now would not reach the
    // box at all: send Escape to the box itself, which is where the guard under test is.
    await user.keyboard("{Escape}");
    fireEvent.keyDown(group, { key: "Escape" });

    expect(screen.getByRole("group", CONFIRM_GROUP)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /해제하는 중/ })).toHaveAttribute("aria-busy", "true");

    getMock.mockResolvedValue(RESTORED);
    finish(RESTORED);
    await waitFor(() => expect(screen.queryByRole("group", CONFIRM_GROUP)).toBeNull());
  });
});

describe("an open confirmation does not outlive the control", () => {
  it("a re-read that shows the project un-archived closes it for good: a later archive does not bring it back", async () => {
    const user = userEvent.setup();
    const { group } = await openConfirmation(user);
    await waitFor(() => expect(document.activeElement).toBe(group));

    getMock.mockResolvedValue(RESTORED);
    await user.click(screen.getByRole("button", { name: "새로 고침" }));
    await waitFor(() => expect(screen.queryByRole("button", TRIGGER)).toBeNull());
    expect(screen.queryByRole("group", CONFIRM_GROUP)).toBeNull();

    getMock.mockResolvedValue(ARCHIVED);
    await user.click(screen.getByRole("button", { name: "새로 고침" }));
    const trigger = await screen.findByRole("button", TRIGGER);

    expect(trigger).toBeEnabled();
    expect(screen.queryByRole("group", CONFIRM_GROUP)).toBeNull();
  });
});

describe("focus after the un-archive ends", () => {
  it("success: the control is gone, so focus lands on the status line that shows the result; the page re-read; the notice is polite", async () => {
    unarchiveMock.mockResolvedValue(RESTORED);
    const user = userEvent.setup();
    const { group } = await openConfirmation(user);
    await waitFor(() => expect(document.activeElement).toBe(group));
    await user.tab();
    getMock.mockResolvedValue(RESTORED);

    await user.keyboard("{Enter}");

    await waitFor(() => expect(screen.queryByRole("button", TRIGGER)).toBeNull());
    await waitFor(() => expect(screen.queryByRole("group", CONFIRM_GROUP)).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(statusLine()));
    expect(statusLine()).toHaveTextContent("운영중");
    // Un-archived through the route, then the project and its controls were read again.
    expect(unarchiveMock).toHaveBeenCalledWith("p1");
    expect(getMock).toHaveBeenCalledTimes(2);
    // `deploy_controls` of the second read is what the page now offers.
    expect(screen.getByRole("button", { name: "배포 확정" })).toBeInTheDocument();
    // The acting owner's own action: announced politely, never as an alert.
    expect(screen.getByText(/보관을 해제했습니다/).closest('[role="status"]')).not.toBeNull();
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
  });

  it("a refusal that leaves the control (500): alert, no re-read, focus back on the control", async () => {
    unarchiveMock.mockRejectedValue(new ApiError(500, "boom", "boom"));
    const user = userEvent.setup();
    const { trigger, group } = await openConfirmation(user);
    await waitFor(() => expect(document.activeElement).toBe(group));
    await user.tab();

    await user.keyboard("{Enter}");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("서버에서 보관 해제를 처리하지 못했습니다");
    await waitFor(() => expect(screen.queryByRole("group", CONFIRM_GROUP)).toBeNull());
    expect(trigger).toBeEnabled();
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(getMock).toHaveBeenCalledTimes(1);
  });

  it("a stale page (400: already un-archived): its sentence, a re-read, and focus on the status line because the control is gone", async () => {
    unarchiveMock.mockRejectedValue(new ApiError(400, "Project is not archived", "Project is not archived"));
    const user = userEvent.setup();
    const { group } = await openConfirmation(user);
    await waitFor(() => expect(document.activeElement).toBe(group));
    await user.tab();
    getMock.mockResolvedValue(RESTORED);

    await user.keyboard("{Enter}");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("이미 보관이 해제된 프로젝트입니다");
    await waitFor(() => expect(screen.queryByRole("button", TRIGGER)).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(statusLine()));
    expect(getMock).toHaveBeenCalledTimes(2);
  });

  it("a 403 whose re-read still shows the control (the viewer is still the owner): focus back on the control", async () => {
    unarchiveMock.mockRejectedValue(new ApiError(403, "Forbidden", "Forbidden"));
    const user = userEvent.setup();
    const { trigger, group } = await openConfirmation(user);
    await waitFor(() => expect(document.activeElement).toBe(group));
    await user.tab();

    await user.keyboard("{Enter}");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("소유자 또는 admin만 보관을 해제할 수 있습니다");
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });
});

// -- the failure alert does not outlive what it was about ----------------------------------

/** A project that is not archived and offers all three deploy controls (the page after a re-read
 * showed it un-archived, and something to confirm, roll back and stop). */
const LIVE = project("ACTIVE", {
  confirmable_build_id: "bld-1",
  confirm_blocked_reason: null,
  rollback_target_id: "dep-0",
  rollback_blocked_reason: null,
  active_deployment_id: "dep-1",
  in_flight: false,
});

/** Opens the confirmation, confirms it, and returns the alert the refusal put on screen (and the
 * mounted view, for a test that changes the viewer). `reread` is what the page's next read of the
 * project answers, for a refusal that re-reads. */
async function failUnarchive(
  user: ReturnType<typeof userEvent.setup>,
  error: ApiError,
  reread: ProjectDetail = ARCHIVED,
) {
  unarchiveMock.mockRejectedValue(error);
  const { group, view } = await openConfirmation(user);
  await waitFor(() => expect(document.activeElement).toBe(group));
  await user.tab();
  getMock.mockResolvedValue(reread);
  await user.keyboard("{Enter}");
  const alert = await screen.findByRole("alert");
  return { alert, view };
}

/** A call that stays pending until `finish()`, so a test can look at the page while it runs. */
function pendingCall() {
  const call = { finish: () => {} };
  const promise = new Promise<never>((resolve) => {
    call.finish = () => resolve(undefined as never);
  });
  return { promise, call };
}

const STALE_400 = () => new ApiError(400, "Project is not archived", "Project is not archived");
const SERVER_500 = () => new ApiError(500, "boom", "boom");

describe("a failed un-archive's alert does not outlive what it was about", () => {
  beforeEach(() => {
    for (const mock of [deploymentsApi.confirm, deploymentsApi.rollback, deploymentsApi.stop]) {
      vi.mocked(mock).mockReset();
    }
    vi.mocked(projectsApi.submitChangeRequest).mockReset();
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("새로 고침 clears it, and the control is still offered, so it was the refresh and not the control going", async () => {
    const user = userEvent.setup();
    const { alert } = await failUnarchive(user, SERVER_500());
    expect(alert).toHaveTextContent("서버에서 보관 해제를 처리하지 못했습니다");
    expect(screen.getByRole("button", TRIGGER)).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "새로 고침" }));

    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(screen.getByRole("button", TRIGGER)).toBeEnabled();
  });

  it.each([
    { action: "배포 확정", mock: () => vi.mocked(deploymentsApi.confirm) },
    { action: "롤백", mock: () => vi.mocked(deploymentsApi.rollback) },
    { action: "중지", mock: () => vi.mocked(deploymentsApi.stop) },
  ])("starting $action clears it, and the alert is gone while the action is still running", async ({ action, mock }) => {
    const { promise, call } = pendingCall();
    mock().mockReturnValue(promise);
    const user = userEvent.setup();
    // The refusal re-reads and the page now shows the project un-archived with its deploy controls:
    // the situation in which the stale alert and a deploy control are on screen together.
    const { alert } = await failUnarchive(user, STALE_400(), LIVE);
    expect(alert).toHaveTextContent("이미 보관이 해제된 프로젝트입니다");
    const control = await screen.findByRole("button", { name: action });
    await waitFor(() => expect(screen.queryByRole("button", TRIGGER)).toBeNull());
    expect(screen.getByRole("alert")).toHaveTextContent("이미 보관이 해제된 프로젝트입니다");

    await user.click(control);

    expect(mock()).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).toBeNull();
    call.finish();
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(3));
  });

  it("sending a change request clears it, and the alert is gone while the request is still running", async () => {
    const { promise, call } = pendingCall();
    vi.mocked(projectsApi.submitChangeRequest).mockReturnValue(promise);
    const user = userEvent.setup();
    const { alert } = await failUnarchive(user, STALE_400(), LIVE);
    expect(alert).toHaveTextContent("이미 보관이 해제된 프로젝트입니다");
    await waitFor(() => expect(screen.queryByRole("button", TRIGGER)).toBeNull());

    await openChangeRequestTab(user);
    await user.type(screen.getByRole("textbox", FORM_FIELD), "목록에 검색창 추가해줘");
    await user.click(screen.getByRole("button", { name: "제출" }));

    expect(vi.mocked(projectsApi.submitChangeRequest)).toHaveBeenCalledTimes(1);
    // The alert lives on the 배포 tab, which is not on screen now, so look where it was: a check
    // made on the form's own tab would pass whether or not the submit had cleared it.
    await user.click(screen.getByRole("tab", { name: "배포" }));
    expect(screen.queryByRole("alert")).toBeNull();
    call.finish();
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(3));
  });

  it("it goes when the control stops being offered for a reason that is not its own (the viewer is switched to someone else)", async () => {
    const user = userEvent.setup();
    const { alert, view } = await failUnarchive(user, SERVER_500());
    expect(alert).toHaveTextContent("서버에서 보관 해제를 처리하지 못했습니다");
    expect(screen.getByRole("button", TRIGGER)).toBeEnabled();

    viewer.current = { id: "u-other", name: "다른 사람", role: "requester" };
    view.rerender(tree());

    await waitFor(() => expect(screen.queryByRole("button", TRIGGER)).toBeNull());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("the refusal that made the control go stays as the explanation of why it is gone: a 400 whose re-read shows it un-archived", async () => {
    const user = userEvent.setup();

    const { alert } = await failUnarchive(user, STALE_400(), RESTORED);

    expect(alert).toHaveTextContent("이미 보관이 해제된 프로젝트입니다");
    await waitFor(() => expect(screen.queryByRole("button", TRIGGER)).toBeNull());
    expect(screen.getByRole("alert")).toHaveTextContent("이미 보관이 해제된 프로젝트입니다");
  });

  it("... and a 403 whose re-read shows the viewer is no longer the owner", async () => {
    const user = userEvent.setup();
    const someoneElsesNow = { ...ARCHIVED, owner_id: "u-someone-else" };

    const { alert } = await failUnarchive(user, new ApiError(403, "Forbidden", "Forbidden"), someoneElsesNow);

    expect(alert).toHaveTextContent("소유자 또는 admin만 보관을 해제할 수 있습니다");
    await waitFor(() => expect(screen.queryByRole("button", TRIGGER)).toBeNull());
    expect(screen.getByRole("alert")).toHaveTextContent("소유자 또는 admin만 보관을 해제할 수 있습니다");
  });

  it("a later un-archive attempt replaces the alert instead of stacking a second one", async () => {
    const user = userEvent.setup();
    await failUnarchive(user, SERVER_500());

    unarchiveMock.mockRejectedValue(new ApiError(404, "Not found", "Not found"));
    await user.click(screen.getByRole("button", TRIGGER));
    await screen.findByRole("group", CONFIRM_GROUP);
    await user.tab();
    await user.keyboard("{Enter}");

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("프로젝트를 찾을 수 없습니다"));
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });
});

describe("the change-request form is out of the tab order until a first deployment is confirmed", () => {
  it("a project with no confirmed ref: the field and the submit button are disabled", async () => {
    getMock.mockResolvedValue({ ...RESTORED, confirmed_ref: null });
    const user = userEvent.setup();
    mount();
    await openChangeRequestTab(user);

    expect(await screen.findByRole("textbox", FORM_FIELD)).toBeDisabled();
    expect(screen.getByRole("button", { name: "제출" })).toBeDisabled();
  });

  it("a project with a confirmed ref: the field is enabled and the button is enabled once there is text", async () => {
    getMock.mockResolvedValue({ ...RESTORED, confirmed_ref: "c0ffee0" });
    const user = userEvent.setup();
    mount();
    await openChangeRequestTab(user);

    const field = await screen.findByRole("textbox", FORM_FIELD);
    expect(field).toBeEnabled();
    await user.type(field, "목록에 검색창 추가해줘");
    expect(screen.getByRole("button", { name: "제출" })).toBeEnabled();
  });

  it("a stale page's submit that the backend refuses shows the same reason as the disabled form", async () => {
    getMock.mockResolvedValue({ ...RESTORED, confirmed_ref: "c0ffee0" });
    const detail = "NO_CONFIRMED_REF: project p1 has no confirmed deployment yet";
    vi.mocked(projectsApi.submitChangeRequest).mockRejectedValue(new ApiError(409, detail, detail));
    const user = userEvent.setup();
    mount();
    await openChangeRequestTab(user);

    await user.type(await screen.findByRole("textbox", FORM_FIELD), "목록에 검색창 추가해줘");
    await user.click(screen.getByRole("button", { name: "제출" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("첫 배포를 확정한 뒤에 변경 요청을 보낼 수 있습니다");
  });
});

describe("the project page's tabs", () => {
  const FORM_READY = { ...RESTORED, confirmed_ref: "c0ffee0" };

  it("opens on 배포, with the tab selected and its panel linked to it", async () => {
    mount();

    const tab = await screen.findByRole("tab", { name: "배포" });

    expect(tab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "변경 요청" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tab", { name: "타임라인" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tabpanel")).toHaveAccessibleName("배포");
    expect(screen.getByRole("heading", { level: 2, name: "배포 제어" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "배포 이력" })).toBeInTheDocument();
  });

  it("is one tab stop: Tab from the selected tab leaves the list, and the arrow keys move between tabs", async () => {
    const user = userEvent.setup();
    mount();
    const deploy = await screen.findByRole("tab", { name: "배포" });
    deploy.focus();

    expect(screen.getByRole("tab", { name: "변경 요청" }).tabIndex).toBe(-1);
    expect(screen.getByRole("tab", { name: "타임라인" }).tabIndex).toBe(-1);

    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "변경 요청" })).toHaveAttribute("aria-selected", "true");
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "변경 요청" }));

    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "타임라인" })).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "변경 요청" })).toHaveAttribute("aria-selected", "true");
  });

  it("a header that says only what was loaded: no deployment reads 없음 and 0회", async () => {
    mount();

    await screen.findByRole("tab", { name: "배포" });

    expect(screen.getByText("마지막 배포").parentElement).toHaveTextContent("없음");
    expect(screen.getByText("배포 횟수").parentElement).toHaveTextContent("0회");
  });

  it("a successful submit switches to 타임라인, takes focus to that tab, and the page was re-read", async () => {
    getMock.mockResolvedValue(FORM_READY);
    vi.mocked(projectsApi.submitChangeRequest).mockResolvedValue(undefined as never);
    const user = userEvent.setup();
    mount();
    await openChangeRequestTab(user);

    await user.type(await screen.findByRole("textbox", FORM_FIELD), "목록에 검색창 추가해줘");
    await user.click(screen.getByRole("button", { name: "제출" }));

    await waitFor(() => expect(screen.getByRole("tab", { name: "타임라인" })).toHaveAttribute("aria-selected", "true"));
    expect(screen.getByRole("heading", { level: 2, name: "타임라인" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", FORM_FIELD)).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("tab", { name: "타임라인" })));
    expect(getMock).toHaveBeenCalledTimes(2);
  });

  it("a refused submit stays on 변경 요청 with its alert and the text the person typed", async () => {
    getMock.mockResolvedValue(FORM_READY);
    vi.mocked(projectsApi.submitChangeRequest).mockRejectedValue(new ApiError(500, "boom", "boom"));
    const user = userEvent.setup();
    mount();
    await openChangeRequestTab(user);

    await user.type(await screen.findByRole("textbox", FORM_FIELD), "목록에 검색창 추가해줘");
    await user.click(screen.getByRole("button", { name: "제출" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("변경 요청 제출에 실패했습니다.");
    expect(screen.getByRole("tab", { name: "변경 요청" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("textbox", FORM_FIELD)).toHaveValue("목록에 검색창 추가해줘");
  });

  it("leaving a tab and coming back keeps the typed text", async () => {
    getMock.mockResolvedValue(FORM_READY);
    const user = userEvent.setup();
    mount();
    await openChangeRequestTab(user);
    await user.type(await screen.findByRole("textbox", FORM_FIELD), "초안");

    await user.click(screen.getByRole("tab", { name: "타임라인" }));
    await user.click(screen.getByRole("tab", { name: "변경 요청" }));

    expect(screen.getByRole("textbox", FORM_FIELD)).toHaveValue("초안");
  });

  it("a re-read of the page's data does not move the reader off the tab they are on", async () => {
    // A build in flight makes the page poll, which is a re-read nobody asked for while the reader is
    // on another tab. It has to be on a tab that is not the default one, or a reset would not show.
    getMock.mockResolvedValue({ ...FORM_READY, deploy_controls: { ...OFFERED, in_flight: true } });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      mount();
      await openChangeRequestTab(user);
      await user.click(screen.getByRole("tab", { name: "타임라인" }));
      expect(getMock).toHaveBeenCalledTimes(1);

      // `act` lets the render that follows the re-read (and its effects) run before the check.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5500);
      });

      expect(getMock.mock.calls.length).toBeGreaterThan(1);
      expect(screen.getByRole("tab", { name: "타임라인" })).toHaveAttribute("aria-selected", "true");
    } finally {
      vi.useRealTimers();
    }
  });

  it("the un-archive confirmation stays in the 배포 tab, in the box beside the history", async () => {
    const user = userEvent.setup();
    const { group } = await openConfirmation(user);

    expect(group.closest('[role="tabpanel"]')).toBe(screen.getByRole("tabpanel"));
    expect(screen.getByRole("heading", { level: 2, name: "배포 제어" }).parentElement).toContainElement(group);
  });

  it("the shell owns the h1: the page adds only h2 box titles", async () => {
    mount();
    await screen.findByRole("tab", { name: "배포" });

    expect(screen.queryAllByRole("heading", { level: 1 })).toHaveLength(0);
    expect(screen.getAllByRole("heading").map((h) => h.tagName)).toEqual(["H2", "H2"]);
  });
});

