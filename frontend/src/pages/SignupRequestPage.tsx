import { useState } from "react";
import { Navigate } from "react-router-dom";

import { localAccountsApi } from "../api/localAccounts";
import { AuthPageShell } from "../components/AuthPageShell";
import { useDocumentTitle } from "../components/documentTitle";
import { SIGNUP_TITLE, documentTitle } from "../components/shellNav";
import { Alert } from "../components/ui/Alert";
import { Button } from "../components/ui/Button";
import { Field } from "../components/ui/Field";
import { Input } from "../components/ui/Input";
import { Skeleton } from "../components/ui/Skeleton";
import { TextLink } from "../components/ui/TextLink";
import { useIdentity } from "../context/IdentityContext";
import {
  SIGNUP_PASSWORD_MAX_BYTES,
  SIGNUP_PASSWORD_MIN_LENGTH,
  isSignupValid,
  signupSubmitFailureMessage,
  validateSignup,
  type SignupFieldErrors,
  type SignupFormValues,
} from "./signup";

const EMPTY_VALUES: SignupFormValues = { email: "", name: "", department: "", password: "" };

/**
 * `/signup` — the signup-**request** form.
 * Outside `RequireIdentity`, a sibling of
 * `/login`: reachable with no identity, exactly like the login form it
 * links back to.
 *
 * Only meaningful in `local` mode (the endpoint behind it does not exist
 * otherwise); a direct visit in `dev`/`oidc` bounces to `/login` rather than
 * rendering a form that can only 404.
 */
export function SignupRequestPage() {
  const { mode, loading } = useIdentity();
  const [values, setValues] = useState<SignupFormValues>(EMPTY_VALUES);
  const [fieldErrors, setFieldErrors] = useState<SignupFieldErrors>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  useDocumentTitle(documentTitle(SIGNUP_TITLE));

  if (loading || mode === null) {
    return (
      <AuthPageShell>
        <Skeleton label="가입 요청 화면을 불러오는 중" />
      </AuthPageShell>
    );
  }
  if (mode !== "local") return <Navigate to="/login" replace />;

  function updateField<K extends keyof SignupFormValues>(key: K, value: string) {
    setValues((prev) => ({ ...prev, [key]: value }));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitError(null);
    const errors = validateSignup(values);
    setFieldErrors(errors);
    if (!isSignupValid(errors)) return;

    setSubmitting(true);
    try {
      // The response is identical whether or
      // not the email already existed. The backend enforces that today by
      // always returning the same `detail` (this is not
      // itself a security gap, unlike the login failure message) — but
      // rendering that string here would make "identical regardless of
      // outcome" depend on the backend continuing to phrase it identically,
      // instead of being true by construction. The fixed sentence below is
      // defined here, never read from `response.detail`, so the property is
      // guaranteed locally instead.
      await localAccountsApi.signup(values);
      setSubmitted(true);
      // Clears the typed password out of this component's state along with
      // everything else — nothing here needs it again, and it is never
      // stored anywhere it would outlive the submit.
      setValues(EMPTY_VALUES);
    } catch (err) {
      // `signupSubmitFailureMessage` (signup.ts): a transport failure says so in Korean, a refusal
      // gets a fixed sentence, and neither the backend's `detail` nor a library's English
      // `message` reaches the screen.
      setSubmitError(signupSubmitFailureMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  if (submitted) {
    return (
      <AuthPageShell>
        <h2 className="text-lg font-bold text-slate-900 dark:text-slate-100">가입 요청 접수</h2>
        <p className="mt-2 text-sm font-medium text-success">
          가입 요청이 접수되었습니다. 관리자가 확인 후 승인 여부를 결정합니다.
        </p>
        <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
          관리자가 검토한 뒤 승인 여부를 알 수 있도록 알림을 보내지는 않습니다. 승인 후 같은
          이메일과 비밀번호로 다시 로그인해 주세요.
        </p>
        <p className="mt-4 text-sm">
          <TextLink to="/login">로그인으로 돌아가기</TextLink>
        </p>
      </AuthPageShell>
    );
  }

  return (
    <AuthPageShell>
      <h2 className="text-lg font-bold text-slate-900 dark:text-slate-100">가입 요청</h2>
      <form onSubmit={handleSubmit} className="mt-4">
        {/* these errors are validateSignup's answer to the submit the user just made, so
            each is announced (errorLive). A field error caused by typing would not be. */}
        <Field label="이메일" htmlFor="signup-email" error={fieldErrors.email} errorLive>
          <Input
            id="signup-email"
            type="email"
            value={values.email}
            onChange={(e) => updateField("email", e.target.value)}
            autoComplete="username"
            required
            invalid={Boolean(fieldErrors.email)}
          />
        </Field>
        <Field label="이름" htmlFor="signup-name" error={fieldErrors.name} errorLive>
          <Input
            id="signup-name"
            value={values.name}
            onChange={(e) => updateField("name", e.target.value)}
            required
            invalid={Boolean(fieldErrors.name)}
          />
        </Field>
        <Field
          label="부서"
          htmlFor="signup-department"
          error={fieldErrors.department}
          errorLive
          hint="관리자가 가입을 승인하기 전 확인하는 참고 정보입니다."
        >
          <Input
            id="signup-department"
            value={values.department}
            onChange={(e) => updateField("department", e.target.value)}
            required
            invalid={Boolean(fieldErrors.department)}
          />
        </Field>
        <Field
          label="비밀번호"
          htmlFor="signup-password"
          error={fieldErrors.password}
          errorLive
          hint={`최소 ${SIGNUP_PASSWORD_MIN_LENGTH}자 이상, UTF-8 기준 ${SIGNUP_PASSWORD_MAX_BYTES}바이트 이하 (영문·숫자 ${SIGNUP_PASSWORD_MAX_BYTES}자, 한글 ${SIGNUP_PASSWORD_MAX_BYTES / 3}자까지).`}
        >
          <Input
            id="signup-password"
            type="password"
            value={values.password}
            onChange={(e) => updateField("password", e.target.value)}
            autoComplete="new-password"
            required
            invalid={Boolean(fieldErrors.password)}
          />
        </Field>
        {submitError && (
          <Alert variant="error" live className="mb-3">
            {submitError}
          </Alert>
        )}
        <Button type="submit" className="w-full" busy={submitting}>
          {submitting ? "요청 중..." : "가입 요청"}
        </Button>
      </form>

      <p className="mt-4 text-sm text-slate-500 dark:text-slate-400">
        이미 계정이 있으신가요?{" "}
        <TextLink to="/login">로그인</TextLink>
      </p>
    </AuthPageShell>
  );
}
