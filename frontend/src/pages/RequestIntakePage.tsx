import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { transportErrorMessage } from "../api/client";
import { projectsApi } from "../api/projects";
import {
  NL_REQUEST_MAX_CHARS,
  PROJECT_NAME_MAX_CHARS,
  isAnyOverLimit,
  lengthError,
  lengthHint,
  lengthNotice,
  tooLongMessage,
} from "../api/requestLimits";
import { teamsApi, templatesApi } from "../api/templates";
import { Alert, Button, EmptyState, Field, Input, Select, Skeleton, Textarea } from "../components/ui";
import { useLoaderOnChange } from "../loading/useLoader";
import type { Team, Template } from "../types/api";

/**
 * What the submit handler says when `POST /projects` fails.
 *
 * `api/client.ts` maps a non-2xx response to a thrown `ApiError`, which
 * `extends Error` — so the previous `err instanceof Error ? err.message :
 * "<Korean>"` always took the `err.message` branch, putting the backend's
 * `detail` on screen instead of the Korean fallback that call site wrote.
 *
 * One failure gets its own Korean message: a 422 saying the request or
 * the name is over its length limit (a stale tab, another client). The inputs
 * below do not cut the text; they keep it, show the count, and disable submit
 * while it is over the limit (`isIntakeSubmitBlocked`), so this message is the
 * backstop for a request that got past them. Anything else is still the fallback.
 */
export function requestIntakeFailureMessage(err: unknown, fallback: string): string {
  return tooLongMessage(err) ?? fallback;
}

/** Submit is blocked while the name or the request is over its limit;
 * the text is never cut to fit. A named predicate so a `node`-env test reaches it. */
export function isIntakeSubmitBlocked(name: string, nlRequest: string): boolean {
  return isAnyOverLimit([
    { text: name, max: PROJECT_NAME_MAX_CHARS },
    { text: nlRequest, max: NL_REQUEST_MAX_CHARS },
  ]);
}

/** What the intake page says when the template and team lists cannot be loaded. A dead
 * server is told as such; anything else is the one plain sentence. Never the server's own text. */
export function intakeLoadFailureMessage(err: unknown): string {
  return transportErrorMessage(err) ?? "템플릿과 팀 목록을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";
}

/** Which of the page's states to show. The form needs both lists: a template and a team are
 * required, so a form over an empty select can only end in the browser's "please fill" tooltip,
 * which explains nothing. Each way the lists can fail to be usable is its own view, with its own
 * words, ahead of the form. A named predicate so a `node`-env test reaches it. */
export type IntakeView = "loading" | "error" | "no-templates" | "no-teams" | "form";

export function intakeViewFor(load: {
  loading: boolean;
  error: string | null;
  lists: { templates: readonly unknown[]; teams: readonly unknown[] } | null;
}): IntakeView {
  if (load.loading) return "loading";
  if (load.error !== null) return "error";
  // Settled with neither a result nor an error cannot happen; if it ever does, waiting is honest
  // and showing the form is not.
  if (load.lists === null) return "loading";
  if (load.lists.templates.length === 0) return "no-templates";
  if (load.lists.teams.length === 0) return "no-teams";
  return "form";
}

export function RequestIntakePage() {
  const navigate = useNavigate();
  const lists = useLoaderOnChange<{ templates: Template[]; teams: Team[] }>(
    async (signal) => {
      const [templates, teams] = await Promise.all([
        templatesApi.list({ signal }),
        teamsApi.list({ signal }),
      ]);
      return { templates, teams };
    },
    [],
    intakeLoadFailureMessage,
  );
  const templates: Template[] = lists.data?.templates ?? [];
  const teams: Team[] = lists.data?.teams ?? [];
  const view = intakeViewFor({ loading: lists.loading, error: lists.error, lists: lists.data });
  const [name, setName] = useState("");
  const [templateId, setTemplateId] = useState("");
  const [teamId, setTeamId] = useState("");
  const [nlRequest, setNlRequest] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The selects start on their first option; a choice the user made replaces it.
  const chosenTemplateId = templateId !== "" ? templateId : (templates[0]?.id ?? "");
  const chosenTeamId = teamId !== "" ? teamId : (teams[0]?.id ?? "");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (isIntakeSubmitBlocked(name, nlRequest)) return;
    setError(null);
    setSubmitting(true);
    try {
      const project = await projectsApi.create({ name, template_id: chosenTemplateId, team_id: chosenTeamId, nl_request: nlRequest });
      navigate(`/projects/${project.id}`);
    } catch (err) {
      setError(requestIntakeFailureMessage(err, "요청 제출에 실패했습니다."));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div>
      {view === "loading" && <Skeleton label="템플릿과 팀 목록을 불러오는 중" />}
      {view === "error" && (
        <div>
          <Alert variant="error" live>
            {lists.error}
          </Alert>
          <Button variant="secondary" className="mt-3" onClick={() => void lists.reload()}>
            다시 시도
          </Button>
        </div>
      )}
      {view === "no-templates" && (
        <EmptyState
          title="사용할 수 있는 템플릿이 없습니다"
          description="플랫폼에 등록된 템플릿이 없어 새 요청을 만들 수 없습니다. 플랫폼 관리자에게 문의해 주세요."
        />
      )}
      {view === "no-teams" && (
        <EmptyState
          title="선택할 수 있는 팀이 없습니다"
          description="플랫폼에 등록된 팀이 없어 새 요청을 만들 수 없습니다. 플랫폼 관리자에게 문의해 주세요."
        />
      )}
      {view === "form" && (
        <form onSubmit={handleSubmit} className="max-w-lg">
          <Field
            label="프로젝트 이름"
            htmlFor="intake-name"
            hint={lengthHint(name, PROJECT_NAME_MAX_CHARS)}
            error={lengthError(name, PROJECT_NAME_MAX_CHARS)}
            errorNotice={lengthNotice("프로젝트 이름")}
            required
          >
            <Input
              id="intake-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              invalid={lengthError(name, PROJECT_NAME_MAX_CHARS) !== undefined}
              required
            />
          </Field>
          <Field label="템플릿" htmlFor="intake-template" required>
            <Select id="intake-template" value={chosenTemplateId} onChange={(e) => setTemplateId(e.target.value)} required>
              {templates.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="팀" htmlFor="intake-team" required>
            <Select id="intake-team" value={chosenTeamId} onChange={(e) => setTeamId(e.target.value)} required>
              {teams.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field
            label="요청 내용 (자연어)"
            htmlFor="intake-nl-request"
            hint={lengthHint(nlRequest, NL_REQUEST_MAX_CHARS)}
            error={lengthError(nlRequest, NL_REQUEST_MAX_CHARS)}
            errorNotice={lengthNotice("요청 내용")}
            required
          >
            <Textarea
              id="intake-nl-request"
              value={nlRequest}
              onChange={(e) => setNlRequest(e.target.value)}
              invalid={lengthError(nlRequest, NL_REQUEST_MAX_CHARS) !== undefined}
              required
              rows={5}
              placeholder="예: 팀원들이 휴가를 신청하고 승인 현황을 볼 수 있는 도구를 만들어줘"
            />
          </Field>
          {error && (
            <Alert variant="error" live>
              {error}
            </Alert>
          )}
          <Button
            type="submit"
            busy={submitting}
            disabled={isIntakeSubmitBlocked(name, nlRequest)}
            className="mt-2"
          >
            {submitting ? "제출 중..." : "요청 제출"}
          </Button>
        </form>
      )}
    </div>
  );
}
