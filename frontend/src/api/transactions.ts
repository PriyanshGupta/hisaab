// Transactions: list, manual entry, corrections (overlay) and undo. Every change
// invalidates the one transactions query.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiError, send, unwrap } from "./client";
import type { components } from "./schema";

export type Transaction = components["schemas"]["TransactionOut"];
export type NewTransaction = components["schemas"]["NewTransaction"];
export type TransactionChange = components["schemas"]["TransactionChange"];
export type EditableField = components["schemas"]["RevertRequest"]["fields"][number];

export const transactionsKey = ["transactions"] as const;

/** The list, newest first; with a range, only those dates (both ends included). */
export function useTransactions(range: { start: string; end: string } | null = null) {
  return useQuery({
    queryKey: [...transactionsKey, "list", range?.start ?? null, range?.end ?? null],
    queryFn: () => unwrap(() =>
        api.GET("/api/transactions", range ? { params: { query: range } } : {})),
    placeholderData: (previous) => previous, // a new range keeps the old list on screen until it arrives
  });
}

function useTransactionsMutation<TArgs, TResult>(fn: (args: TArgs) => Promise<TResult>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => Promise.all([queryClient.invalidateQueries({ queryKey: transactionsKey }),
      queryClient.invalidateQueries({ queryKey: ["review"] })]), // the review queue (api/review.ts) reads them too
  });
}

export function useCreateTransaction() {
  return useTransactionsMutation((body: NewTransaction) => unwrap(() => api.POST("/api/transactions", { body })));
}

export function useCorrectTransaction() {
  return useTransactionsMutation(({ id, change }: { id: string; change: TransactionChange }) => unwrap(() =>
      api.PATCH("/api/transactions/{txn_id}", { params: { path: { txn_id: id } }, body: change }),
    ));
}

export function useUndoCorrection() {
  return useTransactionsMutation(({ id, fields }: { id: string; fields: EditableField[] }) => unwrap(() =>
      api.POST("/api/transactions/{txn_id}/revert", { params: { path: { txn_id: id } }, body: { fields } }),
    ));
}

export function useDeleteTransaction() {
  return useTransactionsMutation(async (id: string) => {
    const { error, response } = await send(() =>
      api.DELETE("/api/transactions/{txn_id}", { params: { path: { txn_id: id }, query: { confirm: true } } }),
    );
    if (!response.ok) throw apiError(response, error);
  });
}

/** Money coming in (shown with a "Cr" suffix, as on bank statements). */
export const isCredit = (t: Transaction) => t.kind === "income" || t.kind === "refund";
