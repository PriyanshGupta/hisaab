// Render the real app (real routes, real query client) at a given path.
import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router";

import { createAppQueryClient } from "../api/queryClient";
import { routes } from "../routes";

export function renderApp(path: string) {
  const queryClient = createAppQueryClient();
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { queryClient, router };
}
