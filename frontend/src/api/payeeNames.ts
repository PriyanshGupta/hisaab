// Captured payee corrections (PRD §5.6): admin only. Promoting names history in the database, so the
// transactions, review and report queries are refreshed too.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiError, send, unwrap } from "./client";
import { reviewKey } from "./review";
import type { components } from "./schema";
import { transactionsKey } from "./transactions";

export type CapturedCorrection = components["schemas"]["CorrectionOut"];
export type PromoteIn = components["schemas"]["PromoteIn"];

export const payeeNamesKey = ["admin", "payee-names"] as const;

export function useCorrections() {
  return useQuery({
    queryKey: payeeNamesKey,
    queryFn: () => unwrap(() => api.GET("/api/admin/payee-names")),
  });
}

export function usePromote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: PromoteIn) => unwrap(() => api.POST("/api/admin/payee-names/promote", { body })),
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
