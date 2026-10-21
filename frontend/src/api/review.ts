// The review queue (PRD §5.5): one list of everything that needs the user, and
// the answers that settle many transactions at once. Every answer refreshes the
// queue, the transactions list and the first-run step (all three move together).
import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema";
import { setupKey } from "./setup";
import { transactionsKey } from "./transactions";

export type ReviewItem = components["schemas"]["ReviewItem"];
export type AutoResult = components["schemas"]["AutoOut"];

export const reviewKey = ["review"] as const;
const latestKey = ["review", "auto", "latest"] as const;

export function refreshReview(queryClient: QueryClient) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: reviewKey }),
    queryClient.invalidateQueries({ queryKey: transactionsKey }),
    queryClient.invalidateQueries({ queryKey: setupKey }),
  ]);
}

function useReviewMutation<TArgs, TResult>(fn: (args: TArgs) => Promise<TResult>) {
  const queryClient = useQueryClient();
  return useMutation({ mutationFn: fn, onSuccess: () => refreshReview(queryClient) });
}

export function useReview() {
  return useQuery({ queryKey: reviewKey, queryFn: () => unwrap(() => api.GET("/api/review")) });
}

export function useCategorizePayee() {
  return useReviewMutation((body: { payee_id: string; category_id: string }) =>
    unwrap(() => api.POST("/api/review/payee-category", { body })));
}

/** Categorize a payee not named yet: name it after its text, then one rule, as one action with one refresh at
 *  the end. As two mutations, the refresh after the naming regrouped the list (text -> payee) and could remove
 *  the box before the category was sent: the pick did nothing until tried again (user, 2026-10-21). */
export function useCategorizeText() {
  return useReviewMutation(async (body: { payee_raw: string; category_id: string }) => {
    const named = await unwrap(() => api.POST("/api/review/name-payee", {
      body: { payee_raw: body.payee_raw, name: body.payee_raw } }));
    return unwrap(() => api.POST("/api/review/payee-category", {
      body: { payee_id: named.payee_id, category_id: body.category_id } }));
  });
}

export function useAssignIdentifier() {
  return useReviewMutation((body: { identifier: string; account_id: string }) =>
    unwrap(() => api.POST("/api/review/identifier", { body })));
}

export function useAssignAccount() {
  return useReviewMutation((body: { transaction_ids: string[]; account_id: string }) =>
    unwrap(() => api.POST("/api/review/assign-account", { body })));
}

export function useNotMyAccount() {
  return useReviewMutation((body: { identifier: string }) =>
    unwrap(() => api.POST("/api/review/not-my-account", { body })));
}

export function useNamePayee() {
  return useReviewMutation((body: { payee_raw?: string; transaction_ids?: string[]; payee_id?: string; name?: string }) =>
    unwrap(() => api.POST("/api/review/name-payee", { body })));
}

export function useAcknowledge() {
  return useReviewMutation((id: string) =>
    unwrap(() => api.POST("/api/transactions/{txn_id}/acknowledge", { params: { path: { txn_id: id } } })));
}

export function useAutoCategorize() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(() => api.POST("/api/review/auto-categorize")),
    onSuccess: () => Promise.all([refreshReview(queryClient), queryClient.invalidateQueries({ queryKey: latestKey })]),
  });
}

export function useLatestBatch() {
  return useQuery({ queryKey: latestKey, queryFn: () => unwrap(() => api.GET("/api/review/auto-categorize/latest")) });
}

export function useUndoBatch() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (batchId: string) => unwrap(() =>
      api.DELETE("/api/review/auto-categorize/{batch_id}", { params: { path: { batch_id: batchId } } })),
    onSuccess: () => Promise.all([refreshReview(queryClient), queryClient.invalidateQueries({ queryKey: latestKey })]),
  });
}

/** The email a transaction came from, as text (only when Hisaab kept it); fetched when asked for. */
export function useEmailText(transactionId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["review", "email", transactionId],
    enabled,
    queryFn: () => unwrap(() => api.GET("/api/review/email/{txn_id}", { params: { path: { txn_id: transactionId } } })),
  });
}
