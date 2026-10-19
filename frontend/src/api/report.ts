// The report for one period. Keyed under transactionsKey: every change that touches transactions
// (a correction, a review answer, a category change) already invalidates that prefix.
import { useQuery } from "@tanstack/react-query";

import { api, apiError, send } from "./client";
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
    queryFn: async () => {
      const { data, error, response } = await send(() =>
        api.GET("/api/report", { params: { query: { start, end } } }));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
    placeholderData: (previous) => previous, // stepping months keeps the last report on screen, no flash
  });
}
