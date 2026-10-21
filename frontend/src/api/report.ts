// The report for one period. Keyed under transactionsKey: every change that touches transactions
// (a correction, a review answer, a category change) already invalidates that prefix.
import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema";
import { transactionsKey } from "./transactions";

export type Report = components["schemas"]["ReportOut"];
export type ReportCategory = components["schemas"]["ReportCategory"];
export type ReportPayee = components["schemas"]["ReportPayee"];
export type ReportTransaction = components["schemas"]["ReportTransaction"];
export type ReportTransfer = components["schemas"]["ReportTransfer"];

export function useReport(start: string, end: string) {
  return useQuery({
    queryKey: [...transactionsKey, "report", start, end],
    queryFn: () => unwrap(() =>
        api.GET("/api/report", { params: { query: { start, end } } })),
    placeholderData: (previous) => previous, // stepping months keeps the last report on screen, no flash
  });
}

export type Trends = components["schemas"]["TrendsOut"];
export type TrendCategory = components["schemas"]["TrendCategory"];
export type RecurringCharge = components["schemas"]["RecurringOut"];

/** The 12 months ending this month, per category (slice 21). Under transactionsKey: edits refresh it. */
export function useTrends() {
  return useQuery({
    queryKey: [...transactionsKey, "trends"],
    queryFn: () => unwrap(() => api.GET("/api/trends")),
  });
}

export function useRecurring() {
  return useQuery({
    queryKey: [...transactionsKey, "recurring"],
    queryFn: () => unwrap(() => api.GET("/api/recurring")),
  });
}
