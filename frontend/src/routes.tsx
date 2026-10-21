// The route table, shared by the app (browser router) and tests (memory router).
import { Navigate, type RouteObject } from "react-router";

import { AdminLayout } from "./admin/AdminLayout";
import { PayeeNamesPage } from "./admin/PayeeNamesPage";
import { ResyncPage } from "./admin/ResyncPage";
import { UsersPage } from "./admin/UsersPage";
import { RequireAuth } from "./auth/RequireAuth";
import { AccountsPage } from "./pages/AccountsPage";
import { AddPage } from "./pages/AddPage";
import { CategoriesPage } from "./pages/CategoriesPage";
import { HomePage } from "./pages/HomePage";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { ReviewPage } from "./pages/ReviewPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SendersPage } from "./pages/SendersPage";
import { SetupPage } from "./pages/SetupPage";
import { StatementsPage } from "./pages/StatementsPage";
import { TemplatesPage } from "./pages/TemplatesPage";
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
      { path: "statements", element: <StatementsPage /> },
      { path: "add", element: <AddPage /> },
      { path: "accounts", element: <AccountsPage /> },
      { path: "categories", element: <CategoriesPage /> },
      { path: "settings", element: <SettingsPage /> },
      { path: "setup", element: <SetupPage /> },
      { path: "review", element: <ReviewPage /> },
      {
        path: "admin",
        element: <AdminLayout />,
        children: [
          { index: true, element: <Navigate to="/admin/templates" replace /> },
          { path: "templates", element: <TemplatesPage /> },
          { path: "senders", element: <SendersPage /> },
          { path: "payee-names", element: <PayeeNamesPage /> },
          { path: "users", element: <UsersPage /> },
          { path: "resync", element: <ResyncPage /> },
        ],
      },
      { path: "templates", element: <Navigate to="/admin/templates" replace /> }, // slice 22's address
      { path: "senders", element: <Navigate to="/admin/senders" replace /> }, // slice 23's address
      { path: "categorize", element: <Navigate to="/review?lane=categories" replace /> }, // slice 16's landing
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];
