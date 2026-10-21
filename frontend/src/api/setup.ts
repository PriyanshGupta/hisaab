// First-run setup (tech-design §5.4): the server says which step the user is
// on; the backfill step polls until the first sync ends. Account, Gmail and
// payee-rule changes refresh it (they all move the step).
import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema";

export type Setup = components["schemas"]["SetupOut"];

export const setupKey = ["setup"] as const;
const POLL_MS = 1000;

export function useSetup() {
  return useQuery({
    queryKey: setupKey,
    queryFn: () => unwrap(() => api.GET("/api/setup")),
    refetchInterval: (query) => (query.state.data?.step === "backfill" ? POLL_MS : false),
  });
}
