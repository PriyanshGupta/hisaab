// User management (PRD §6.9): admin only. The API is slice 3's /api/admin/users.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiError, send } from "./client";
import type { components } from "./schema";

export type ManagedUser = components["schemas"]["ManagedUser"];
export type NewUser = components["schemas"]["NewUser"];

export const usersKey = ["admin", "users"] as const;

export function useUsers() {
  return useQuery({
    queryKey: usersKey,
    queryFn: async () => {
      const { data, error, response } = await send(() => api.GET("/api/admin/users"));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
  });
}

export function useAddUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: NewUser) => {
      const { data, error, response } = await send(() => api.POST("/api/admin/users", { body }));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
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
    mutationFn: async ({ id, role }: { id: string; role: string }) => {
      const { data, error, response } = await send(() =>
        api.PATCH("/api/admin/users/{user_id}", { params: { path: { user_id: id } }, body: { role } }));
      if (data === undefined) throw apiError(response, error);
      return data;
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: usersKey }),
  });
}
