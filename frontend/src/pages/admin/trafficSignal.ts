/**
 * The one place the console decides which of the five traffic-signal states it is in.
 * **Whether live services are exempt is the backend's value and is never recomputed here**;
 * this only derives the reason shown beside it.
 *
 * What tells the states apart is carried by two things in the response: the backend's
 * `project_lifecycle_live_services_exempt` verdict and whether the two timestamps are null.
 *   exempt=false, timestamps null     -> Traefik does not route (the proxy records traffic)
 *   exempt=true,  null, !configured   -> Traefik routes, signal off (no TRAEFIK_METRICS_PORT)
 *   exempt=true,  null, configured    -> configured but never read successfully
 *   exempt=true,  a timestamp present -> signal on, not yet usable
 *   exempt=false, a timestamp present -> signal on and usable
 */
import type { PlatformConfigurationValues } from "../../types/api";

/** the last poll must be no older than this. Fixed, not a setting. */
export const TRAFFIC_SIGNAL_MAX_POLL_AGE_MS = 3 * 60 * 1000;

const DAY_MS = 24 * 60 * 60 * 1000;

export type TrafficSignalUnmet = "stale_read" | "window_too_short";

export type TrafficSignalState =
  | { kind: "traefik_not_routing" }
  | { kind: "signal_off" }
  | { kind: "configured_never_read" }
  | {
      kind: "not_yet_usable";
      /** Computed reasons; empty when none explains the backend's verdict. */
      unmet: TrafficSignalUnmet[];
      observedSince: string | null;
      lastPollAt: string | null;
    }
  | { kind: "usable"; observedSince: string | null; lastPollAt: string | null };

type SignalInput = Pick<
  PlatformConfigurationValues,
  | "project_lifecycle_live_services_exempt"
  | "traffic_signal_configured"
  | "project_lifecycle_idle_threshold_days"
  | "traffic_signal_observed_since"
  | "traffic_signal_last_poll_at"
>;

function ms(iso: string | null): number | null {
  if (iso === null) return null;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : t;
}

export function trafficSignalState(values: SignalInput, now: Date): TrafficSignalState {
  const observedSince = values.traffic_signal_observed_since;
  const lastPollAt = values.traffic_signal_last_poll_at;
  const exempt = values.project_lifecycle_live_services_exempt;

  if (observedSince === null && lastPollAt === null) {
    if (!exempt) return { kind: "traefik_not_routing" };
    return values.traffic_signal_configured ? { kind: "configured_never_read" } : { kind: "signal_off" };
  }
  if (!exempt) return { kind: "usable", observedSince, lastPollAt };

  const poll = ms(lastPollAt);
  const since = ms(observedSince);
  const unmet: TrafficSignalUnmet[] = [];
  if (poll === null || now.getTime() - poll > TRAFFIC_SIGNAL_MAX_POLL_AGE_MS) unmet.push("stale_read");
  if (since === null || now.getTime() - since < values.project_lifecycle_idle_threshold_days * DAY_MS) {
    unmet.push("window_too_short");
  }
  return { kind: "not_yet_usable", unmet, observedSince, lastPollAt };
}

/**
 * the reason shown on the "활성 서비스의 유휴 판정 제외" row, derived from the same state as
 * the section below it so the two can never name different causes. The yes/no itself is the
 * backend's `exempt` and is passed in, not recomputed.
 */
export function exemptionReason(state: TrafficSignalState, exempt: boolean): string {
  const verdict = exempt ? "예" : "아니오";
  switch (state.kind) {
    case "traefik_not_routing":
      return `${verdict} — Traefik을 경유하지 않아 서비스 프록시가 트래픽을 기록하므로, 활성 배포도 유휴 기준을 그대로 적용합니다`;
    case "signal_off":
      return `${verdict} — Traefik 경유 시 트래픽 신호가 꺼져 있어, 활성 배포가 있는 프로젝트는 플래그·중지·보관하지 않습니다`;
    case "configured_never_read":
      return `${verdict} — 트래픽 신호가 설정되었으나 아직 읽지 못해, 활성 배포가 있는 프로젝트는 플래그·중지·보관하지 않습니다`;
    case "not_yet_usable":
      return `${verdict} — 트래픽 신호가 아직 사용할 수 없어, 활성 배포가 있는 프로젝트는 플래그·중지·보관하지 않습니다 (아래 트래픽 신호 참고)`;
    case "usable":
      return `${verdict} — 트래픽 신호가 유효하므로, 활성 배포도 유휴 기준에 따라 판정합니다`;
  }
}
