// Accounts and identifiers: server state via TanStack Query. Every change
// invalidates the one accounts query, so all views of accounts stay in step.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiError, send } from "./client";
import type { components } from "./schema";

export type Account = components["schemas"]["AccountOut"];
export type AccountType = Account["type"];
export type NewAccount = components["schemas"]["NewAccount"];
export type AccountChange = components["schemas"]["AccountChange"];

export const accountsKey = ["accounts"] as const;

/** All the user's accounts, archived included (the Accounts screen decides what to show). */
export function useAccounts() {
  return useQuery({
    queryKey: accountsKey,
    queryFn: async () => {
      const { data, error, response } = await send(() =>
        api.GET("/api/accounts", { params: { query: { include_archived: true } } }),
      );
      if (data === undefined) throw apiError(response, error);
      return data;
    },
  });
}

function useAccountsMutation<TArgs, TResult>(fn: (args: TArgs) => Promise<TResult>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: accountsKey }),
  });
}

export function useCreateAccount() {
  return useAccountsMutation(async (body: NewAccount) => {
    const { data, error, response } = await send(() => api.POST("/api/accounts", { body }));
    if (data === undefined) throw apiError(response, error);
    return data;
  });
}

export function useUpdateAccount() {
  return useAccountsMutation(async ({ id, change }: { id: string; change: AccountChange }) => {
    const { data, error, response } = await send(() =>
      api.PATCH("/api/accounts/{account_id}", { params: { path: { account_id: id } }, body: change }),
    );
    if (data === undefined) throw apiError(response, error);
    return data;
  });
}

export function useArchiveAccount() {
  return useAccountsMutation(async (id: string) => {
    const { data, error, response } = await send(() =>
      api.POST("/api/accounts/{account_id}/archive", { params: { path: { account_id: id } } }),
    );
    if (data === undefined) throw apiError(response, error);
    return data;
  });
}

export function useAddUpiId() {
  return useAccountsMutation(async ({ id, upiId }: { id: string; upiId: string }) => {
    const { data, error, response } = await send(() =>
      api.POST("/api/accounts/{account_id}/identifiers", {
        params: { path: { account_id: id } },
        body: { kind: "vpa", identifier: upiId },
      }),
    );
    if (data === undefined) throw apiError(response, error);
    return data;
  });
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
