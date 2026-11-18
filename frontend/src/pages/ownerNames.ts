/**
 * The registry's owner column: a project row carries `owner_id`, and the name is resolved in
 * the browser from the user directory (`GET /users`, readable by every authenticated user; a
 * non-admin's view keeps every active user and any deactivated one that still owns a project, per
 * `services/user_directory`). No API field was added for this.
 *
 * An owner the directory does not return is **unresolved** (`null`), never the raw id: an id is
 * meaningless to a reader and shown as if it were a name it would mislead.
 */
import { visibleControls } from "../components/ui/visibleControls";
import type { User } from "../types/api";

/** The word shown where an owner cannot be resolved. */
export const UNRESOLVED_OWNER_LABEL = "확인할 수 없음";

export type OwnerNames = ReadonlyMap<string, string>;

export function ownerNamesFrom(users: readonly Pick<User, "id" | "name">[]): OwnerNames {
  // A Map, not a plain object: an id such as `constructor` must not resolve to a function.
  const names = new Map<string, string>();
  for (const user of users) {
    // A user's name is a string someone typed: it is stored already passed through
    // `visibleControls`, so a control or direction-changing character shows as a marker.
    const shown = visibleControls(String(user.name ?? ""));
    if (shown.trim() !== "") names.set(user.id, shown);
  }
  return names;
}

/** The owner's name, or null when this directory does not know the id. */
export function ownerNameFor(names: OwnerNames, ownerId: string): string | null {
  return names.get(ownerId) ?? null;
}
