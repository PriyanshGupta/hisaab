// Statement upload (tech-design §7): upload with a password, preview, pick the account, discard, merge.
// The merge is a job: POST returns its id and the page polls it with useJob (api/gmail.ts), which also
// refreshes transactions and review when it ends.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiError, send } from "./client";
import type { components } from "./schema";

export type StatementPreview = components["schemas"]["StatementPreview"];
export type PreviewRow = components["schemas"]["PreviewRow"];
export type MatchRow = components["schemas"]["MatchRow"];
export type StagedSummary = components["schemas"]["StagedSummary"];

export const statementsKey = ["statements"] as const;
const openKey = [...statementsKey, "open"] as const;
const previewKey = (id: string) => [...statementsKey, "preview", id] as const;

export function useOpenStatements() {
  return useQuery({
    queryKey: openKey,
    queryFn: async () => {
      const { data, error, response } = await send(() => api.GET("/api/statements"));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
  });
}

export function useStatement(id: string) {
  return useQuery({
    queryKey: previewKey(id),
    queryFn: async () => {
      const { data, error, response } = await send(() =>
        api.GET("/api/statements/{staging_id}", { params: { path: { staging_id: id } } }));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
    retry: false, // a 404 means expired or discarded: say so at once
  });
}

/** The file as base64, in chunks (String.fromCharCode on a whole PDF would overflow the stack). */
async function toBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

export function useUploadStatement() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ file, password }: { file: File; password: string }) => {
      const body = { filename: file.name, password: password || null, content_base64: await toBase64(file) };
      const { data, error, response } = await send(() => api.POST("/api/statements", { body }));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(previewKey(data.id), data); // the page opens it without a second read
      return queryClient.invalidateQueries({ queryKey: openKey });
    },
  });
}

export function usePickAccount(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (accountId: string) => {
      const { data, error, response } = await send(() => api.PATCH("/api/statements/{staging_id}",
        { params: { path: { staging_id: id } }, body: { account_id: accountId } }));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(previewKey(id), data);
      return queryClient.invalidateQueries({ queryKey: openKey });
    },
  });
}

/** Discarding (or merging) ends a preview: drop it rather than re-read it, which would 404. */
export function useForgetStatement() {
  const queryClient = useQueryClient();
  return (id: string) => {
    queryClient.removeQueries({ queryKey: previewKey(id) });
    return queryClient.invalidateQueries({ queryKey: openKey });
  };
}

export function useDiscardStatement() {
  const forget = useForgetStatement();
  return useMutation({
    mutationFn: async (id: string) => {
      const { error, response } = await send(() =>
        api.DELETE("/api/statements/{staging_id}", { params: { path: { staging_id: id } } }));
      if (!response.ok) throw apiError(response, error);
    },
    onSuccess: (_data, id) => forget(id),
  });
}

export function useMergeStatement(id: string) {
  return useMutation({
    mutationFn: async (acceptGap: boolean) => {
      const { data, error, response } = await send(() => api.POST("/api/statements/{staging_id}/merge",
        { params: { path: { staging_id: id } }, body: { accept_gap: acceptGap } }));
      if (data === undefined) throw apiError(response, error);
      return data.job_id;
    },
  });
}
