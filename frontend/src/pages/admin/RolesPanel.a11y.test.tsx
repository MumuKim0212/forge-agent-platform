// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment 2026-08-30: the role console's per-row apply button is named for its
 * user. Every row's button said `적용`, so a screen reader's list of controls, or a voice command,
 * could not tell whose role it changed (the select beside it was already `<name> 역할`). Scope is
 * the accessible name only. One mount check sits beside it: the sentence naming the admin count
 * must carry the API's number, not a recount of the rows (the only way to tell the two apart is to
 * mount the panel with rows and an answer that disagree).
 *
 * The api module and the identity hook are mocked.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { usersApi } from "../../api/users";
import type { User } from "../../types/api";
import { RolesPanel } from "./RolesPanel";

vi.mock("../../api/users", () => ({
  usersApi: { list: vi.fn(), changeRole: vi.fn(), usableAdminCount: vi.fn() },
}));
vi.mock("../../context/IdentityContext", () => ({
  useIdentity: () => ({
    currentUser: { id: "u-admin", name: "관리자", role: "admin" },
    refreshCurrentUser: vi.fn(),
  }),
}));

function user(id: string, name: string, role: string): User {
  return { id, name, email: `${id}@example.test`, role, team_id: null, created_at: "2026-10-01T00:00:00Z" };
}

beforeEach(() => {
  vi.mocked(usersApi.list)
    .mockReset()
    .mockResolvedValue([
      user("u-admin", "관리자", "admin"),
      user("u-1", "홍길동", "requester"),
      user("u-2", "김철수", "approver"),
    ]);
  vi.mocked(usersApi.changeRole).mockReset();
  vi.mocked(usersApi.usableAdminCount).mockReset().mockResolvedValue({ count: 1 });
});

describe("RolesPanel: every apply button is named for its row", () => {
  it("names each button for its user, beside the select named the same way, and no two share a name", async () => {
    render(<RolesPanel />);

    for (const name of ["관리자", "홍길동", "김철수"]) {
      expect(await screen.findByRole("button", { name: `${name} 적용` })).toHaveTextContent("적용");
      expect(screen.getByRole("combobox", { name: `${name} 역할` })).toBeInTheDocument();
    }
    const names = screen
      .getAllByRole("button")
      .map((button) => button.getAttribute("aria-label"))
      .filter((label): label is string => label !== null);
    expect(names).toHaveLength(3);
    expect(new Set(names).size).toBe(names.length);
  });

  it("keeps the name and the visible text in step while a change is being saved", async () => {
    vi.mocked(usersApi.changeRole).mockReturnValue(new Promise(() => {}));
    render(<RolesPanel />);
    const select = await screen.findByRole("combobox", { name: "홍길동 역할" });
    fireEvent.change(select, { target: { value: "approver" } });

    fireEvent.click(screen.getByRole("button", { name: "홍길동 적용" }));

    const saving = await screen.findByRole("button", { name: "홍길동 변경 중..." });
    expect(saving).toHaveTextContent("변경 중...");
    expect(screen.getByRole("button", { name: "김철수 적용" })).toBeInTheDocument();
  });
});

describe("RolesPanel: the admin count is the API's, not a recount of the rows", () => {
  it("shows the number the API returned while three rows hold the admin role", async () => {
    vi.mocked(usersApi.list).mockResolvedValue([
      user("u-admin", "관리자", "admin"),
      user("u-off", "퇴사자", "admin"),
      user("u-nopw", "비번없음", "admin"),
      user("u-1", "홍길동", "requester"),
    ]);
    vi.mocked(usersApi.usableAdminCount).mockResolvedValue({ count: 1 });

    render(<RolesPanel />);

    expect(await screen.findByText(/현재 활성 admin: 1명\./)).toBeInTheDocument();
    expect(screen.queryByText(/현재 활성 admin: 3명/)).not.toBeInTheDocument();
  });

  it("follows the API value, not the rows: the same rows with a different answer show that answer", async () => {
    vi.mocked(usersApi.usableAdminCount).mockResolvedValue({ count: 7 });

    render(<RolesPanel />);

    expect(await screen.findByText(/현재 활성 admin: 7명\./)).toBeInTheDocument();
  });

  it("shows no count when the read fails, rather than counting rows", async () => {
    vi.mocked(usersApi.usableAdminCount).mockRejectedValue(new Error("boom"));

    render(<RolesPanel />);

    await screen.findByRole("combobox", { name: "홍길동 역할" });
    expect(screen.queryByText(/현재 활성 admin/)).not.toBeInTheDocument();
  });
});
