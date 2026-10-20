// Balances and the reconciliation check (tech-design §8.3, §8.4). Keyed under transactionsKey, so every
// correction, review answer, merge and sync refreshes the balances and their gaps along with the report.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiError, send } from "./client";
import type { components } from "./schema";
import { transactionsKey } from "./transactions";

export type AccountBalance = components["schemas"]["AccountBalance"];
export type BalanceGap = components["schemas"]["Gap"];

export const balancesKey = [...transactionsKey, "balances"] as const;

export function useBalances() {
  return useQuery({
    queryKey: balancesKey,
    queryFn: async () => {
      const { data, error, response } = await send(() => api.GET("/api/balances"));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
  });
}

function useBalanceMutation<TArgs>(fn: (args: TArgs) => Promise<AccountBalance>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: fn,
    // an accepted gap writes a transaction (the list); every change can move the report and the balances
    onSuccess: () => queryClient.invalidateQueries({ queryKey: transactionsKey }),
  });
}

export function useCheckpoint(accountId: string) {
  return useBalanceMutation(async (body: { balance_paise: number; on: string }) => {
    const { data, error, response } = await send(() => api.POST("/api/accounts/{account_id}/checkpoints",
      { params: { path: { account_id: accountId } }, body }));
    if (data === undefined) throw apiError(response, error);
    return data;
  });
}

export function useResolveGap(action: "accept" | "close") {
  return useBalanceMutation(async (observationId: string) => {
    const path = { params: { path: { observation_id: observationId } } };
    const { data, error, response } = await send(() => action === "accept"
      ? api.POST("/api/balances/checks/{observation_id}/accept", path)
      : api.POST("/api/balances/checks/{observation_id}/close", path));
    if (data === undefined) throw apiError(response, error);
    return data;
  });
}
