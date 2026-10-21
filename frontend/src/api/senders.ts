// The sender review queue (PRD §6.10, slice 23): admin only. Approving starts each inbox's backfill of the
// sender's mail in the background; the transactions it brings arrive through the normal sync pipeline.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiError, send, unwrap } from "./client";
import type { components } from "./schema";

export type CandidateSender = components["schemas"]["CandidateSender"];
export type FoundIn = components["schemas"]["FoundIn"];
export type SweepExample = components["schemas"]["SweepExample"];

export const sendersKey = ["admin", "senders"] as const;

export function useSenderQueue() {
  return useQuery({
    queryKey: sendersKey,
    queryFn: () => unwrap(() => api.GET("/api/admin/senders")),
  });
}

export function useApproveSender() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => unwrap(() =>
        api.POST("/api/admin/senders/{sender_id}/approve", { params: { path: { sender_id: id } } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: sendersKey }),
  });
}

export function useIgnoreSender() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      const { error, response } = await send(() =>
        api.POST("/api/admin/senders/{sender_id}/ignore", { params: { path: { sender_id: id } } }));
      if (!response.ok) throw apiError(response, error);
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: sendersKey }),
  });
}

export function useRunSweep() {
  return useMutation({
    mutationFn: async () => {
      const { data, error, response } = await send(() => api.POST("/api/admin/senders/sweep"));
      if (data === undefined) throw apiError(response, error);
      return data.jobs.length;
    },
  });
}
