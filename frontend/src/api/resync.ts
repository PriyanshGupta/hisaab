// Re-sync a date range, and the parser-change notices that suggest one (tech-design §5.2): admin only.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiError, send } from "./client";
import type { components } from "./schema";

export type ResyncNotice = components["schemas"]["ResyncNoticeOut"];
export type ResyncPreview = components["schemas"]["ResyncPreview"];

export const noticesKey = ["admin", "resync", "notices"] as const;

export function useResyncNotices() {
  return useQuery({
    queryKey: noticesKey,
    queryFn: async () => {
      const { data, error, response } = await send(() => api.GET("/api/admin/resync/notices"));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
  });
}

export function useDismissNotice() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      const { error, response } = await send(() =>
        api.POST("/api/admin/resync/notices/{notice_id}/dismiss", { params: { path: { notice_id: id } } }));
      if (!response.ok) throw apiError(response, error);
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: noticesKey }),
  });
}

export type ResyncRange = { start: string; end: string; user_id: string | null };

export function useResyncPreview() {
  return useMutation({
    mutationFn: async ({ start, end, user_id }: ResyncRange) => {
      const { data, error, response } = await send(() => api.GET("/api/admin/resync/preview",
        { params: { query: { start, end, ...(user_id ? { user_id } : {}) } } }));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
  });
}

export function useStartResync() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: ResyncRange) => {
      const { data, error, response } = await send(() => api.POST("/api/admin/resync", { body }));
      if (data === undefined) throw apiError(response, error);
      return data.jobs.length;
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: noticesKey }),
  });
}
