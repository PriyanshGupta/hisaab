import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";

import { ApiError } from "./client";
import { meQueryKey } from "./session";

/**
 * One QueryClient per app (and per test). Any request answered 401 means the
 * session ended (expired, or reset by an admin), so the cached user is cleared
 * and RequireAuth sends the person to sign-in, wherever they were.
 */
export function createAppQueryClient(): QueryClient {
  const onError = (error: unknown) => {
    if (error instanceof ApiError && error.status === 401) client.setQueryData(meQueryKey, null);
  };
  const client: QueryClient = new QueryClient({
    queryCache: new QueryCache({ onError }),
    mutationCache: new MutationCache({ onError }),
    defaultOptions: {
      queries: {
        // Retry only what retrying can fix: network blips and 5xx, never 4xx.
        retry: (failures, error) => !(error instanceof ApiError && error.status < 500) && failures < 2,
        refetchOnWindowFocus: true,
      },
    },
  });
  return client;
}
