import { describe, expect, it } from "vitest";

import { exemptionReason, trafficSignalState } from "./trafficSignal";

const NOW = new Date("2026-11-12T12:00:00Z");
const THRESHOLD = 90;
const ago = (ms: number) => new Date(NOW.getTime() - ms).toISOString();
const DAY = 24 * 60 * 60 * 1000;
const MIN = 60 * 1000;

function values(over: {
  exempt: boolean;
  since: string | null;
  poll: string | null;
  days?: number;
  configured?: boolean;
}) {
  return {
    project_lifecycle_live_services_exempt: over.exempt,
    traffic_signal_configured: over.configured ?? over.since !== null,
    project_lifecycle_idle_threshold_days: over.days ?? THRESHOLD,
    traffic_signal_observed_since: over.since,
    traffic_signal_last_poll_at: over.poll,
  };
}

describe("trafficSignalState", () => {
  it("Traefik does not route: not exempt, timestamps null", () => {
    expect(trafficSignalState(values({ exempt: false, since: null, poll: null }), NOW).kind).toBe(
      "traefik_not_routing",
    );
  });

  it("signal off: exempt, both timestamps null", () => {
    expect(trafficSignalState(values({ exempt: true, since: null, poll: null }), NOW).kind).toBe("signal_off");
  });

  it("configured but never read: exempt, configured, timestamps null (not 'signal off')", () => {
    const s = trafficSignalState(values({ exempt: true, since: null, poll: null, configured: true }), NOW);
    expect(s.kind).toBe("configured_never_read");
    const off = trafficSignalState(values({ exempt: true, since: null, poll: null, configured: false }), NOW);
    expect(off.kind).toBe("signal_off");
  });

  it("usable: not exempt with timestamps", () => {
    const s = trafficSignalState(values({ exempt: false, since: ago(100 * DAY), poll: ago(MIN) }), NOW);
    expect(s.kind).toBe("usable");
  });

  it("not yet usable, stale read alone", () => {
    const s = trafficSignalState(values({ exempt: true, since: ago(100 * DAY), poll: ago(10 * MIN) }), NOW);
    expect(s).toMatchObject({ kind: "not_yet_usable", unmet: ["stale_read"] });
  });

  it("not yet usable, window too short alone", () => {
    const s = trafficSignalState(values({ exempt: true, since: ago(5 * DAY), poll: ago(MIN) }), NOW);
    expect(s).toMatchObject({ kind: "not_yet_usable", unmet: ["window_too_short"] });
  });

  it("not yet usable, both conditions", () => {
    const s = trafficSignalState(values({ exempt: true, since: ago(5 * DAY), poll: ago(10 * MIN) }), NOW);
    expect(s).toMatchObject({ kind: "not_yet_usable", unmet: ["stale_read", "window_too_short"] });
  });

  it("backend says exempt but no computed reason explains it: verdict without a reason", () => {
    const s = trafficSignalState(values({ exempt: true, since: ago(100 * DAY), poll: ago(MIN) }), NOW);
    expect(s).toMatchObject({ kind: "not_yet_usable", unmet: [] });
  });

  it("never contradicts the backend: stale-looking data with exempt=false stays usable", () => {
    const s = trafficSignalState(values({ exempt: false, since: ago(DAY), poll: ago(60 * MIN) }), NOW);
    expect(s.kind).toBe("usable");
  });

  it("3-minute boundary: exactly 3 minutes is fresh, one millisecond more is stale", () => {
    const at = trafficSignalState(values({ exempt: true, since: ago(100 * DAY), poll: ago(3 * 60 * 1000) }), NOW);
    const over = trafficSignalState(values({ exempt: true, since: ago(100 * DAY), poll: ago(3 * 60 * 1000 + 1) }), NOW);
    expect(at).toMatchObject({ unmet: [] });
    expect(over).toMatchObject({ unmet: ["stale_read"] });
  });

  it("window boundary: exactly one threshold watched is enough", () => {
    const at = trafficSignalState(values({ exempt: true, since: ago(90 * DAY), poll: ago(MIN) }), NOW);
    const under = trafficSignalState(values({ exempt: true, since: ago(90 * DAY - 1), poll: ago(MIN) }), NOW);
    expect(at).toMatchObject({ unmet: [] });
    expect(under).toMatchObject({ unmet: ["window_too_short"] });
  });
});

describe("offset-carrying timestamps (the backend sends aware UTC)", () => {
  it("a 30 s old poll with an explicit offset is fresh whatever the machine's zone", () => {
    const s = trafficSignalState(
      values({ exempt: true, since: "2026-11-07T12:00:00+00:00", poll: "2026-11-12T11:59:30Z", days: 90 }),
      NOW,
    );
    expect(s).toMatchObject({ kind: "not_yet_usable", unmet: ["window_too_short"] });
  });
});

describe("exemptionReason: per state, never names a cause the section contradicts", () => {
  const state = (v: Parameters<typeof values>[0]) => trafficSignalState(values(v), NOW);

  it("Traefik not routing: the proxy records traffic", () => {
    const text = exemptionReason(state({ exempt: false, since: null, poll: null }), false);
    expect(text).toContain("아니오");
    expect(text).toContain("서비스 프록시가 트래픽을 기록");
  });

  it("signal off: names the signal being off", () => {
    const text = exemptionReason(state({ exempt: true, since: null, poll: null, configured: false }), true);
    expect(text).toContain("예");
    expect(text).toContain("꺼져 있어");
  });

  it("configured, never read: does not claim there is no signal", () => {
    const text = exemptionReason(state({ exempt: true, since: null, poll: null, configured: true }), true);
    expect(text).toContain("설정되었으나 아직 읽지 못해");
    expect(text).not.toContain("꺼져");
  });

  it("not yet usable: points at the section, claims no proxy and no missing signal", () => {
    const text = exemptionReason(state({ exempt: true, since: ago(5 * DAY), poll: ago(MIN) }), true);
    expect(text).toContain("아직 사용할 수 없어");
    expect(text).not.toContain("서비스 프록시");
    expect(text).not.toContain("꺼져");
  });

  it("usable: judged by the threshold, no proxy claim", () => {
    const text = exemptionReason(state({ exempt: false, since: ago(100 * DAY), poll: ago(MIN) }), false);
    expect(text).toContain("아니오");
    expect(text).toContain("트래픽 신호가 유효");
    expect(text).not.toContain("서비스 프록시");
  });
});
