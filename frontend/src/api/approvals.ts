import { api, type RequestOptions } from "./client";
import { pathSegment } from "./path";
import type {
  ApprovalDecisionInput,
  ApprovalRequest,
  ApprovalRequestSummary,
} from "../types/api";

export const approvalsApi = {
  list: (status?: string, options?: RequestOptions) =>
    api.get<ApprovalRequestSummary[]>(
      status ? `/approvals?status=${encodeURIComponent(status)}` : "/approvals",
      options,
    ),
  get: (id: string, options?: RequestOptions) =>
    api.get<ApprovalRequest>(`/approvals/${pathSegment(id)}`, options),
  approve: (id: string, input: ApprovalDecisionInput = {}) =>
    api.post<ApprovalRequest>(`/approvals/${pathSegment(id)}/approve`, input),
  reject: (id: string, input: ApprovalDecisionInput) =>
    api.post<ApprovalRequest>(`/approvals/${pathSegment(id)}/reject`, input),
};
