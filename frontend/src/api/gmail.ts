// Gmail connection and sync (tech-design §5.5). A sync is a background job:
// POST returns its id and the screen polls GET /api/jobs/{id} until it ends.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiError, send } from "./client";
import type { components } from "./schema";

export type GmailStatus = components["schemas"]["GmailStatus"];
export type Job = components["schemas"]["JobOut"];

export const gmailKey = ["gmail"] as const;
const POLL_MS = 500;

export function useGmailStatus() {
  return useQuery({
    queryKey: gmailKey,
    queryFn: async () => {
      const { data, error, response } = await send(() => api.GET("/api/gmail"));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
  });
}

/** Resolves to Google's consent URL; the caller sends the browser there. */
export function useConnectGmail() {
  return useMutation({
    mutationFn: async () => {
      const { data, error, response } = await send(() => api.POST("/api/gmail/connect"));
      if (data === undefined) throw apiError(response, error);
      return data.authorization_url;
    },
  });
}

export function useSyncNow() {
  return useMutation({
    mutationFn: async () => {
      const { data, error, response } = await send(() => api.POST("/api/gmail/sync"));
      if (data === undefined) throw apiError(response, error);
      return data.job_id;
    },
  });
}

const finished = (job: Job | undefined) => job !== undefined && job.state !== "queued" && job.state !== "running";

/** Polls a job until it finishes; then refreshes the Gmail status (the sync clock may have moved). */
export function useJob(jobId: string | null) {
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: ["jobs", jobId],
    enabled: jobId !== null,
    queryFn: async () => {
      const { data, error, response } = await send(() =>
        api.GET("/api/jobs/{job_id}", { params: { path: { job_id: jobId ?? "" } } }),
      );
      if (data === undefined) throw apiError(response, error);
      if (finished(data)) await queryClient.invalidateQueries({ queryKey: gmailKey });
      return data;
    },
    refetchInterval: (query) => (finished(query.state.data) ? false : POLL_MS),
  });
}
