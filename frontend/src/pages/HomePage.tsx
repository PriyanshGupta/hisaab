// The report is the home screen (PRD §10.5). Slice 18 builds it; for now the
// route exists and says plainly why it is empty.
export function HomePage() {
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
