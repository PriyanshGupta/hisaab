// Captured payee corrections (PRD §5.6): admin only. Promoting names history in the database, so the
// transactions, review and report queries are refreshed too.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiError, send } from "./client";
import { reviewKey } from "./review";
import type { components } from "./schema";
import { transactionsKey } from "./transactions";

export type CapturedCorrection = components["schemas"]["CorrectionOut"];
export type PromoteIn = components["schemas"]["PromoteIn"];

export const payeeNamesKey = ["admin", "payee-names"] as const;

export function useCorrections() {
  return useQuery({
    queryKey: payeeNamesKey,
    queryFn: async () => {
      const { data, error, response } = await send(() => api.GET("/api/admin/payee-names"));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
  });
}

export function usePromote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: PromoteIn) => {
      const { data, error, response } = await send(() => api.POST("/api/admin/payee-names/promote", { body }));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: payeeNamesKey });
      void qc.invalidateQueries({ queryKey: transactionsKey });
      void qc.invalidateQueries({ queryKey: reviewKey });
    },
  });
}

export function useDismissCorrection() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: { key: string; payee_id: string }) => {
      const { error, response } = await send(() => api.POST("/api/admin/payee-names/dismiss", { body }));
      if (!response.ok) throw apiError(response, error);
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: payeeNamesKey }),
  });
}
