import { NOT_FOUND_TITLE } from "../components/shellNav";
import { Card, TextLink } from "../components/ui";

export const NOT_FOUND_HEADING = NOT_FOUND_TITLE;

/**
 * The catch-all screen. It is a *state of the address*, not something
 * the user just did, so it carries no live region (`role="alert"`): a screen
 * reader gets the new document title and heading on navigation, and the
 * sentence is read in order. Rendered inside the app shell, so it is only ever
 * reached by a signed-in user; an anonymous visit to an unknown path is
 * redirected to login by `RequireIdentity` like any protected path, which
 * keeps the 404 from revealing which paths exist.
 */
export function NotFoundPage() {
  return (
    <Card className="px-6 py-8 text-center">
      <h2 className="text-lg font-bold text-slate-900 dark:text-slate-100">{NOT_FOUND_HEADING}</h2>
      <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
        주소가 잘못되었거나 존재하지 않는 페이지입니다.
      </p>
      <TextLink to="/projects" className="mt-4 inline-block text-sm">
        프로젝트 목록으로 돌아가기
      </TextLink>
    </Card>
  );
}
