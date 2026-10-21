# Hisaab — personal finance tracker

## Documents (precedence order)
1. `docs/build_docs/tech-design.md` — the engineering contract. Authoritative on stack, schema, and flows.
2. `docs/build_docs/build-plan.md` — the slice order and per-slice verification steps.
3. `docs/build_docs/prd.md` — product requirements. §11 is a decision log; read it before proposing any product change.

`tech-design.md` §10 lists amendments to the PRD. Where the two disagree, the tech design wins.

## Build status

Every slice in `build-plan.md` (0–25) has been built; each has its plan in `docs/superpowers/plans/`.
**Read `docs/superpowers/BUILD_LOG.md` first:** it has the status of every slice, the verifications still open,
and what changed from each plan. Running the app day to day (backups, restore, CSV export, the systemd service,
phone access) is in `OPERATIONS.md`. `docs/` is local only, so those files exist only on the build machine.

## Fixtures

`./fixtures/` holds real bank artifacts for seeding parsers:
- `fixtures/emails/` — UPI and credit card alerts (HDFC, ICICI, SBI, Scapia)
- `fixtures/statements/` — credit card statements (HDFC, ICICI, SBI, Scapia)

These are the source for the v0 seed templates and the initial normalization patterns.

**Transform vocabulary is derived from these fixtures and nothing else.** Do not add spec
vocabulary or registry transforms for formats not present here (`tech-design.md` §4.4).

Redact before any fixture is committed or sent to an LLM: account numbers, card numbers,
VPAs, names, addresses. Use consistent fake values so patterns still match.

## How work proceeds
- **One slice per session.** Never start a second slice without being asked.
- Every slice has a verification step in `build-plan.md`. It is not optional. "Looks right" is not verification.
- A slice is not done until its verification has actually been run and its output seen.

## Per-slice protocol

Every slice follows the same shape, so it won't be repeated in each prompt:

- `superpowers:writing-plans` before starting
- `superpowers:test-driven-development` while implementing
- `superpowers:systematic-debugging` on any bug or unexpected behaviour
- `superpowers:verification-before-completion` before claiming done, which
  includes actually running that slice's verification step from `build-plan.md`
  and showing me the output
- Build the named slice only. Never start the next one unasked.
- If something in `tech-design.md` turns out wrong once you're in the detail,
  stop and tell me. Don't work around it silently.
- When reporting a verification, paste the actual command and its actual output.
  Never summarise it as "tests pass" or "verified". If a check produces a number
  (a count, a parse rate, a balance), state the number.
- For the four verify-hard slices (2, 11, 15, 20), also tell me the one thing I
  should check by hand to confirm your test isn't passing vacuously.

## Settled decisions — do not re-open
The stack was decided with reasoning recorded in `tech-design.md` §11. Python 3.14, FastAPI,
SQLite in WAL mode, SQLAlchemy 2.0 + Alembic (batch mode), React 19 + TypeScript + Vite,
TanStack Query, google-re2, pdfplumber. Do not propose alternatives — §11 records why each
alternative was rejected. If you believe one is genuinely wrong, say so with reasoning and stop;
do not change it unilaterally.

Do not use brainstorming or ideation skills on this project. The design phase is complete.

## Hard constraints
- The SQLite `.db` file must live on the Linux filesystem (`/home/...`), never under `/mnt/c`.
  POSIX locking is broken over the Windows mount — this is a data corruption path.
- The Anthropic API key goes in a project-scoped `.env` only. Never add `export ANTHROPIC_API_KEY`
  to any shell profile, rc file, or setup script.
- All money is integer paise in `BIGINT` columns. No floating point in the accounting path, ever.
- Every user-scoped query filters on `user_id`.
