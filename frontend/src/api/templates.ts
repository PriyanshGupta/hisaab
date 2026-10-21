// The template promotion queue (PRD §6.8, slice 22): admin only. Approving re-reads emails and can move
// transactions into review, so it refreshes those queries too.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiError, send } from "./client";
import type { components } from "./schema";
import { noticesKey } from "./resync";
import { reviewKey } from "./review";
import { transactionsKey } from "./transactions";

export type Candidate = components["schemas"]["Candidate"];
export type CandidateExample = components["schemas"]["CandidateExample"];
export type ReplayField = components["schemas"]["ReplayField"];
export type Approved = components["schemas"]["Approved"];
export type Excluded = components["schemas"]["Excluded"];

export const templatesKey = ["admin", "templates"] as const;

export function useTemplateQueue() {
  return useQuery({
    queryKey: templatesKey,
    queryFn: async () => {
      const { data, error, response } = await send(() => api.GET("/api/admin/templates"));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
  });
}

export function useApproveTemplate() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      const { data, error, response } = await send(() =>
        api.POST("/api/admin/templates/{template_id}/approve", { params: { path: { template_id: id } } }));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: templatesKey });
      void qc.invalidateQueries({ queryKey: transactionsKey });
      void qc.invalidateQueries({ queryKey: reviewKey });
    },
  });
}

export function useRejectTemplate() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      const { error, response } = await send(() =>
        api.POST("/api/admin/templates/{template_id}/reject", { params: { path: { template_id: id } } }));
      if (!response.ok) throw apiError(response, error);
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: templatesKey }),
  });
}

export function useIncludeFormat() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      const { error, response } = await send(() =>
        api.POST("/api/admin/templates/formats/{sighting_id}/include", { params: { path: { sighting_id: id } } }));
      if (!response.ok) throw apiError(response, error);
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: templatesKey }),
  });
}

export type TemplateInUse = components["schemas"]["TemplateInUse"];
export const inUseKey = ["admin", "templates", "active"] as const;

export function useTemplatesInUse() {
  return useQuery({
    queryKey: inUseKey,
    queryFn: async () => {
      const { data, error, response } = await send(() => api.GET("/api/admin/templates/active"));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
  });
}

/** Save a learned template's spec; the server writes the re-sync notice the banner shows. */
export function useEditSpec() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, spec }: { id: string; spec: Record<string, unknown> }) => {
      const { data, error, response } = await send(() =>
        api.PUT("/api/admin/templates/{template_id}/spec", { params: { path: { template_id: id } }, body: { spec } }));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: inUseKey });
      void qc.invalidateQueries({ queryKey: noticesKey });
    },
  });
}
