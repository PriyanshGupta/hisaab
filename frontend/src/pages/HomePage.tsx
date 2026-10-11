// The report is the home screen (PRD §10.5). Slice 18 builds it; for now the
// route exists and says plainly why it is empty. Until first-run setup is
// through, home is the setup (or, once the backfill is in and nothing is
// categorized yet, bulk categorization): never an empty report (§5.4).
import { Navigate } from "react-router";

import { describeError } from "../api/client";
import { useSetup } from "../api/setup";

export function HomePage() {
  const setup = useSetup();
  if (setup.isPending) return <p className="muted">Loading…</p>;
  if (setup.isError) return <p className="form-error" role="alert">{describeError(setup.error)}</p>;
  if (setup.data.step === "categorize") return <Navigate to="/categorize" replace />;
  if (setup.data.step !== "done") return <Navigate to="/setup" replace />;
  return (
    <>
      <header className="page-head">
        <h1 className="page-title">Report</h1>
      </header>
      <section className="empty">
        <p className="empty__lead">Nothing to report yet.</p>
        <p className="empty__body">
          Spending by category appears here once your accounts are added and Gmail is connected.
        </p>
      </section>
    </>
  );
}
