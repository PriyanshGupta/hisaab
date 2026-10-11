import "@fontsource-variable/anek-latin/wdth.css"; // bundled into dist/: no CDN, works offline
import "./styles/tokens.css";
import "./styles/base.css";

import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider, createBrowserRouter } from "react-router";

import { createAppQueryClient } from "./api/queryClient";
import { routes } from "./routes";

const queryClient = createAppQueryClient();
const router = createBrowserRouter(routes);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
