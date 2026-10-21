// User management (PRD §6.9): admin only. The API is slice 3's /api/admin/users.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiError, send, unwrap } from "./client";
import type { components } from "./schema";

export type ManagedUser = components["schemas"]["ManagedUser"];
export type NewUser = components["schemas"]["NewUser"];

export const usersKey = ["admin", "users"] as const;

export function useUsers() {
  return useQuery({
    queryKey: usersKey,
    queryFn: () => unwrap(() => api.GET("/api/admin/users")),
  });
}

export function useAddUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: NewUser) => unwrap(() => api.POST("/api/admin/users", { body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: usersKey }),
  });
}

export function useResetPassword() {
  return useMutation({
    mutationFn: async ({ id, password }: { id: string; password: string }) => {
      const { error, response } = await send(() =>
        api.POST("/api/admin/users/{user_id}/password", { params: { path: { user_id: id } }, body: { password } }));
      if (!response.ok) throw apiError(response, error);
    },
  });
}

export function useChangeRole() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, role }: { id: string; role: string }) => unwrap(() =>
        api.PATCH("/api/admin/users/{user_id}", { params: { path: { user_id: id } }, body: { role } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: usersKey }),
  });
}
