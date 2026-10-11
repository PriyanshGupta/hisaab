import { Link } from "react-router";

export function NotFoundPage() {
  return (
    <>
      <header className="page-head">
        <h1 className="page-title">Page not found</h1>
      </header>
      <section className="empty">
        <p className="empty__body">
          This address doesn't match anything in Hisaab. <Link to="/">Go to the report</Link>.
        </p>
      </section>
    </>
  );
}
