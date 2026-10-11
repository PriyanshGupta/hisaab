import { ApiError } from "./client";
import { createAppQueryClient } from "./queryClient";
import { meQueryKey } from "./session";

const ASHA = { id: "u-1", username: "asha", display_name: "Asha", role: "admin" };

test("any query answered 401 signs the user out of the SPA", async () => {
  const client = createAppQueryClient();
  client.setQueryData(meQueryKey, ASHA);
  await client
    .fetchQuery({ queryKey: ["anything"], queryFn: () => Promise.reject(new ApiError(401, "Not logged in")) })
    .catch(() => undefined);
  expect(client.getQueryData(meQueryKey)).toBeNull();
});

test("other failures leave the session alone", async () => {
  const client = createAppQueryClient();
  client.setQueryData(meQueryKey, ASHA);
  await client
    .fetchQuery({ queryKey: ["x"], queryFn: () => Promise.reject(new ApiError(404, "Nope")), retry: false })
    .catch(() => undefined);
  expect(client.getQueryData(meQueryKey)).toEqual(ASHA);
});
