// Balances and the reconciliation check (tech-design §8.3, §8.4). Keyed under transactionsKey, so every
// correction, review answer, merge and sync refreshes the balances and their gaps along with the report.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema";
import { transactionsKey } from "./transactions";

export type AccountBalance = components["schemas"]["AccountBalance"];
export type BalanceGap = components["schemas"]["Gap"];

export const balancesKey = [...transactionsKey, "balances"] as const;

export function useBalances() {
  return useQuery({
    queryKey: balancesKey,
    queryFn: () => unwrap(() => api.GET("/api/balances")),
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
  return useBalanceMutation((body: { balance_paise: number; on: string }) => unwrap(() =>
    api.POST("/api/accounts/{account_id}/checkpoints", { params: { path: { account_id: accountId } }, body })));
}

export function useResolveGap(action: "accept" | "close") {
  return useBalanceMutation(async (observationId: string) => {
    const path = { params: { path: { observation_id: observationId } } };
    return unwrap(() => action === "accept"
      ? api.POST("/api/balances/checks/{observation_id}/accept", path)
      : api.POST("/api/balances/checks/{observation_id}/close", path));
  });
}
