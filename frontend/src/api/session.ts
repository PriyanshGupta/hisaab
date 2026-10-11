// Who is signed in, and signing in and out. The session itself is an HttpOnly
// cookie the browser manages; the SPA only ever asks the server who it is.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { type SessionUser, ApiError, NetworkError, api, apiError, send } from "./client";

export const meQueryKey = ["auth", "me"] as const;

/** The signed-in user, or null when there is no valid session (a 401 is an answer, not an error). */
async function fetchMe(): Promise<SessionUser | null> {
  const { data, error, response } = await send(() => api.GET("/api/auth/me"));
  if (response.status === 401) return null;
  if (data === undefined) throw apiError(response, error);
  return data;
}

export function useMe() {
  return useQuery({ queryKey: meQueryKey, queryFn: fetchMe, staleTime: 60_000 });
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (credentials: { username: string; password: string }) => {
      const { data, error, response } = await send(() => api.POST("/api/auth/login", { body: credentials }));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
    onSuccess: (user) => queryClient.setQueryData(meQueryKey, user),
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      await send(() => api.POST("/api/auth/logout"));
    },
    // Even if the request failed, forget everything this browser tab knew.
    onSettled: () => {
      queryClient.clear();
      queryClient.setQueryData(meQueryKey, null);
    },
  });
}

/** What the sign-in form says when signing in fails. */
export function loginErrorMessage(error: unknown): string {
  if (error instanceof NetworkError) return "Can't reach the Hisaab server. Check that it's running, then try again.";
  if (error instanceof ApiError) {
    if (error.status === 401) return "Wrong username or password.";
    if (error.status === 503) return "Hisaab is busy finishing a long job. Try again in a minute.";
    return `Sign-in failed (error ${error.status}). Try again.`;
  }
  return "Sign-in failed. Try again.";
}
