// The route table, shared by the app (browser router) and tests (memory router).
import { Navigate, type RouteObject } from "react-router";

import { RequireAuth } from "./auth/RequireAuth";
import { AccountsPage } from "./pages/AccountsPage";
import { AddPage } from "./pages/AddPage";
import { CategoriesPage } from "./pages/CategoriesPage";
import { HomePage } from "./pages/HomePage";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { ReviewPage } from "./pages/ReviewPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SetupPage } from "./pages/SetupPage";
import { TransactionsPage } from "./pages/TransactionsPage";
import { AppShell } from "./shell/AppShell";

export const routes: RouteObject[] = [
  { path: "/login", element: <LoginPage /> },
  {
    element: (
      <RequireAuth>
        <AppShell />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <HomePage /> },
      { path: "transactions", element: <TransactionsPage /> },
      { path: "add", element: <AddPage /> },
      { path: "accounts", element: <AccountsPage /> },
      { path: "categories", element: <CategoriesPage /> },
      { path: "settings", element: <SettingsPage /> },
      { path: "setup", element: <SetupPage /> },
      { path: "review", element: <ReviewPage /> },
      { path: "categorize", element: <Navigate to="/review?lane=categories" replace /> }, // slice 16's landing
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];
