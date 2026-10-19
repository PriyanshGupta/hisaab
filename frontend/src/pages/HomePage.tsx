// The report is the home screen (PRD §10.5). Until first-run setup is through,
// home is the setup (or, once the backfill is in and nothing is categorized
// yet, bulk categorization): never an empty report (§5.4).
import { Navigate } from "react-router";

import { describeError } from "../api/client";
import { useSetup } from "../api/setup";
import { ReportPage } from "./ReportPage";

export function HomePage() {
  const setup = useSetup();
  if (setup.isPending) return <p className="muted">Loading…</p>;
  if (setup.isError) return <p className="form-error" role="alert">{describeError(setup.error)}</p>;
  if (setup.data.step === "categorize") return <Navigate to="/review?lane=categories" replace />;
  if (setup.data.step !== "done") return <Navigate to="/setup" replace />;
  return <ReportPage />;
}
