import { api } from "./client";
import type { NotificationTestResult } from "../types/api";

/**
 * The console's one notification action (`routers/notifications.py`): send a test message
 * to every configured channel. `admin`-only and audited. It lives outside `/api/console`, which is
 * read-only by construction; the channels' state is read from `GET /console/health`.
 *
 * Answers 409 when no channel is configured and 429 when one was sent a moment ago.
 */
export const NOTIFICATION_TEST_PATH = "/platform/notifications/test";

export const notificationsApi = {
  sendTest: () => api.post<NotificationTestResult>(NOTIFICATION_TEST_PATH),
};
