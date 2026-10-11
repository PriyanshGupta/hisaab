// Categories (per user), payees (global) and payee rules. A transaction's
// category is resolved on the server at read time, so any change here also
// refreshes the transactions query.
import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiError, send } from "./client";
import type { components } from "./schema";
import { setupKey } from "./setup";
import { transactionsKey } from "./transactions";

export type Category = components["schemas"]["CategoryOut"];
export type Payee = components["schemas"]["PayeeOut"];

export const categoriesKey = ["categories"] as const;
export const payeesKey = ["payees"] as const;

export function useCategories() {
  return useQuery({
    queryKey: categoriesKey,
    queryFn: async () => {
      const { data, error, response } = await send(() => api.GET("/api/categories"));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
  });
}

export function usePayees() {
  return useQuery({
    queryKey: payeesKey,
    queryFn: async () => {
      const { data, error, response } = await send(() => api.GET("/api/payees"));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
  });
}

function refreshAfter(queryClient: QueryClient) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: categoriesKey }),
    queryClient.invalidateQueries({ queryKey: payeesKey }),
    queryClient.invalidateQueries({ queryKey: transactionsKey }),
    queryClient.invalidateQueries({ queryKey: setupKey }), // the first-run step and the categorize groups
  ]);
}

function useCategoryMutation<TArgs, TResult>(fn: (args: TArgs) => Promise<TResult>) {
  const queryClient = useQueryClient();
  return useMutation({ mutationFn: fn, onSuccess: () => refreshAfter(queryClient) });
}

async function unwrap<T>(call: () => Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  const { data, error, response } = await send(call);
  if (data === undefined) throw apiError(response, error);
  return data;
}

export function useCreateCategory() {
  return useCategoryMutation((name: string) => unwrap(() => api.POST("/api/categories", { body: { name } })));
}

export function useRenameCategory() {
  return useCategoryMutation(({ id, name }: { id: string; name: string }) =>
    unwrap(() => api.PATCH("/api/categories/{category_id}", { params: { path: { category_id: id } }, body: { name } })),
  );
}

export function useArchiveCategory() {
  return useCategoryMutation((id: string) =>
    unwrap(() => api.POST("/api/categories/{category_id}/archive", { params: { path: { category_id: id } } })),
  );
}

export function useRestoreCategory() {
  return useCategoryMutation((id: string) =>
    unwrap(() => api.POST("/api/categories/{category_id}/restore", { params: { path: { category_id: id } } })),
  );
}

/** File every transaction of a payee under a category, for this user (null removes the rule). */
export function useSetPayeeCategory() {
  return useCategoryMutation(({ payeeId, categoryId }: { payeeId: string; categoryId: string | null }) =>
    unwrap(() =>
      api.PUT("/api/payees/{payee_id}/category", {
        params: { path: { payee_id: payeeId } },
        body: { category_id: categoryId },
      }),
    ),
  );
}
