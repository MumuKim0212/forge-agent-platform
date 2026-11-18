/** the wording of a preview that failed to start. Literals, per code (contract strings). */
import { describe, expect, it } from "vitest";

import {
  asPreviewStartFailure,
  previewStartFailureLabel,
  previewStartFailureNextStep,
} from "./previewStartFailure";

describe("previewStartFailureLabel", () => {
  it.each([
    ["NO_IMAGE", "프리뷰를 시작하지 못함 (빌드 이미지 없음)"],
    ["IMAGE_MISSING", "프리뷰를 시작하지 못함 (필요한 이미지가 서버에 없음)"],
    ["NETWORK_FAILED", "프리뷰를 시작하지 못함 (프리뷰 네트워크 생성 실패)"],
    ["DATA_SERVICES_FAILED", "프리뷰를 시작하지 못함 (프리뷰용 데이터 서비스 생성 실패)"],
    ["CONTAINER_START_FAILED", "프리뷰를 시작하지 못함 (컨테이너 시작 실패)"],
    ["SIGNING_SECRET_MISSING", "프리뷰를 시작하지 못함 (프리뷰 서명 설정 누락)"],
    ["UNEXPECTED", "프리뷰를 시작하지 못함 (예기치 못한 오류)"],
  ])("%s has its own label", (code, label) => {
    expect(previewStartFailureLabel(code)).toBe(label);
  });

  it("renders a generic label, not a blank, for a code it does not know", () => {
    expect(previewStartFailureLabel("SOMETHING_NEW")).toBe("프리뷰를 시작하지 못함");
    expect(previewStartFailureLabel("")).not.toBe("");
  });

  it("does not look a code up on the object prototype", () => {
    expect(previewStartFailureLabel("toString")).toBe("프리뷰를 시작하지 못함");
  });
});

describe("previewStartFailureNextStep", () => {
  it("names the operator action for a missing image", () => {
    expect(previewStartFailureNextStep("IMAGE_MISSING")).toContain("이미지");
    expect(previewStartFailureNextStep("IMAGE_MISSING")).toContain("운영자 조치");
  });

  it("has none for a code with no known fix, and for an unknown one", () => {
    expect(previewStartFailureNextStep("CONTAINER_START_FAILED")).toBeNull();
    expect(previewStartFailureNextStep("SOMETHING_NEW")).toBeNull();
  });
});

describe("asPreviewStartFailure", () => {
  it("accepts a non-empty string and nothing else", () => {
    expect(asPreviewStartFailure("IMAGE_MISSING")).toBe("IMAGE_MISSING");
    expect(asPreviewStartFailure(null)).toBeNull();
    expect(asPreviewStartFailure(undefined)).toBeNull();
    expect(asPreviewStartFailure("")).toBeNull();
    expect(asPreviewStartFailure(3)).toBeNull();
  });
});
