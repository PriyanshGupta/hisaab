// Who is signed in, signing up, and signing in and out. The session itself is an HttpOnly
// cookie the browser manages; the SPA only ever asks the server who it is.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, apiError, describeError, send, type SessionUser, unwrap } from "./client";

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
    mutationFn: (credentials: { username: string; password: string }) =>
      unwrap(() => api.POST("/api/auth/login", { body: credentials })),
    onSuccess: (user) => queryClient.setQueryData(meQueryKey, user),
  });
}

export const signupInfoKey = ["auth", "signup"] as const;

/** Whether the next account to sign up becomes the admin (nobody has signed up yet). */
export function useSignupInfo() {
  return useQuery({ queryKey: signupInfoKey, queryFn: () => unwrap(() => api.GET("/api/auth/signup")), staleTime: 60_000 });
}

export function useSignup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (account: { username: string; display_name: string; password: string }) =>
      unwrap(() => api.POST("/api/auth/signup", { body: account })),
    onSuccess: (user) => {
      queryClient.setQueryData(meQueryKey, user);
      void queryClient.invalidateQueries({ queryKey: signupInfoKey });
    },
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

/** What the sign-in form says when signing in fails: the shared sentences, plus the 401 case. */
export function loginErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) return "Wrong username or password.";
  return describeError(error); // network, busy (503), locked out (429: the server's sentence)
}
