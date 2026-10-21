// Accounts and identifiers: server state via TanStack Query. Every change
// invalidates the one accounts query, so all views of accounts stay in step.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiError, send, unwrap } from "./client";
import type { components } from "./schema";
import { setupKey } from "./setup";
import { transactionsKey } from "./transactions";

export type Account = components["schemas"]["AccountOut"];
export type AccountType = Account["type"];
export type NewAccount = components["schemas"]["NewAccount"];
export type AccountChange = components["schemas"]["AccountChange"];

export const accountsKey = ["accounts"] as const;

/** All the user's accounts, archived included (the Accounts screen decides what to show). */
export function useAccounts() {
  return useQuery({
    queryKey: accountsKey,
    queryFn: () => unwrap(() =>
        api.GET("/api/accounts", { params: { query: { include_archived: true } } }),
      ),
  });
}

function useAccountsMutation<TArgs, TResult>(fn: (args: TArgs) => Promise<TResult>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => Promise.all([queryClient.invalidateQueries({ queryKey: accountsKey }),
      queryClient.invalidateQueries({ queryKey: setupKey }),
      queryClient.invalidateQueries({ queryKey: ["review"] }), // a new account resolves its last 4 in review
      queryClient.invalidateQueries({ queryKey: transactionsKey })]), // ...and moves those legs, so the report too
  });
}

export function useCreateAccount() {
  return useAccountsMutation((body: NewAccount) => unwrap(() => api.POST("/api/accounts", { body })));
}

export function useUpdateAccount() {
  return useAccountsMutation(({ id, change }: { id: string; change: AccountChange }) => unwrap(() =>
      api.PATCH("/api/accounts/{account_id}", { params: { path: { account_id: id } }, body: change }),
    ));
}

export function useArchiveAccount() {
  return useAccountsMutation((id: string) => unwrap(() =>
      api.POST("/api/accounts/{account_id}/archive", { params: { path: { account_id: id } } }),
    ));
}

/** Another number, UPI ID or name the bank's alerts use for this account, kind worked out by the server
 *  (review's own "ending 4821 is my HDFC savings"). Earlier transactions that mention it move here. */
export function useAddIdentifier() {
  return useAccountsMutation(({ id, identifier }: { id: string; identifier: string }) => unwrap(() =>
      api.POST("/api/review/identifier", { body: { account_id: id, identifier } }),
    ));
}

export function useRemoveIdentifier() {
  return useAccountsMutation(async ({ id, identifierId }: { id: string; identifierId: string }) => {
    const { error, response } = await send(() =>
      api.DELETE("/api/accounts/{account_id}/identifiers/{identifier_id}", {
        params: { path: { account_id: id, identifier_id: identifierId } },
      }),
    );
    if (!response.ok) throw apiError(response, error);
  });
}

export const TYPE_LABELS: Record<AccountType, string> = {
  bank: "Bank account",
  credit_card: "Credit card",
  cash: "Cash",
};

/** What the opening amount means for each type, worded so a card's debt is entered positive. */
export const OPENING_LABELS: Record<AccountType, string> = {
  bank: "Balance",
  credit_card: "Amount you owed",
  cash: "Cash in hand",
};
