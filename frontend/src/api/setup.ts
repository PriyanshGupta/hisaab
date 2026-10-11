// First-run setup (tech-design §5.4): the server says which step the user is
// on; the backfill step polls until the first sync ends. Account, Gmail and
// payee-rule changes refresh it (they all move the step).
import { useQuery } from "@tanstack/react-query";

import { api, apiError, send } from "./client";
import type { components } from "./schema";

export type Setup = components["schemas"]["SetupOut"];
export type Uncategorized = components["schemas"]["UncategorizedOut"];

export const setupKey = ["setup"] as const;
const POLL_MS = 1000;

export function useSetup() {
  return useQuery({
    queryKey: setupKey,
    queryFn: async () => {
      const { data, error, response } = await send(() => api.GET("/api/setup"));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
    refetchInterval: (query) => (query.state.data?.step === "backfill" ? POLL_MS : false),
  });
}

export function useUncategorized() {
  return useQuery({
    queryKey: [...setupKey, "uncategorized"],
    queryFn: async () => {
      const { data, error, response } = await send(() => api.GET("/api/setup/uncategorized"));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
  });
}
