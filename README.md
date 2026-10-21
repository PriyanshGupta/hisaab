# Hisaab

**See where your money goes without typing in a single transaction.**

Hisaab (Hindi/Urdu for "the accounts", as in *keeping hisaab*) is a personal finance tracker for households in
India. It reads the alert emails your bank already sends for every UPI payment, card purchase and transfer. It
turns them into one ledger, sorts the spending into categories, and shows where the money went. You run it on
your own computer, and everyone in the house can use it from their phone or laptop, each seeing only their own
money.

## Why it exists

Most people never get a clear picture of their spending. Money leaves through UPI apps, two or three credit
cards and bank transfers, and each payment shows up only as one alert email among hundreds. Nothing adds them up
by category, by shop or over time.

Budgeting apps try to fix this by asking you to log every expense by hand, and that rarely lasts more than a
few weeks. Hisaab starts from a simpler observation: **the data already arrives on its own.** Every payment
produces an email. It only needs to be collected, understood and presented.

So Hisaab is built for one thing: **awareness**. Questions like:

- What did I spend last month, on what, and with whom?
- How does that compare with the month before?
- Which payments repeat every month?
- Do my account balances still agree with what the bank says?

It deliberately doesn't do budgets, investments or net worth. Doing one job well beats doing four badly.

## How it works

1. **You connect Gmail (read-only).** Hisaab only reads mail from known bank senders. It can't send, delete or
   change anything in your inbox.
2. **It brings in your history.** On first run it fetches the last three months of bank alerts. After that it
   checks for new ones every six hours, or whenever you press *Sync now*.
3. **It reads each email.**
   - **Known formats:** emails from formats Hisaab knows are parsed on your machine by templates, with no AI
     and nothing sent anywhere.
   - **Unfamiliar formats:** these go to Claude (Anthropic's AI) to pull out the amount, date, payee and
     account.
   - **Learning new formats:** once a new format has been seen three times, Hisaab proposes a template for
     it. The template is checked against those emails, and after an admin approves it, that format is read
     locally from then on.
4. **It ties the pieces together.**
   - The same payment reported twice (an alert and a statement line, say) becomes one transaction.
   - Money moving between two of your own accounts is recognised as a transfer, not spending.
   - Refunds reduce the category they came from.
5. **You teach it, once.** The first time a shop appears, you pick its category. Hisaab remembers it for every
   later payment, and can suggest categories for the rest in bulk.
6. **You read the report.** Spending by category and by shop, against the same period before, with trends and
   recurring payments.

Credit card **statements** (PDF) can be uploaded too. Hisaab checks that the statement's own arithmetic adds up
before merging it, so a misread PDF can't quietly corrupt your numbers.

## What you get

| Screen | What it's for |
|---|---|
| **Report** | Your home screen. Spending (or all cash out) for a month, week, day or any custom range, by category and by shop, compared with the period before. Includes trends per category, recurring payments, account balances, and a CSV download. |
| **Review** | One queue for everything that needs you: shops waiting for a category, alerts whose account Hisaab couldn't place, and figures that look off. Works well with the keyboard. |
| **Transactions** | Search and filter every transaction. Correct an amount or date, or change a category. Your corrections are kept separately from what the bank said, so the original is never lost. |
| **Add** | Record a cash payment or anything the bank never emails about. You can delete these later; bank transactions can only be corrected. |
| **Statements** | Upload a card or bank statement PDF (including password-protected ones; the password is never stored). |
| **Accounts** | Your bank accounts, cards and cash, with opening balances and the last digits that bank alerts use to find them. |
| **Categories** | Rename, add or archive categories. Everyone starts with a sensible default set. |
| **Settings** | The Gmail connection and *Sync now*. |
| **Admin** | For whoever runs the instance: approving new email templates and newly discovered bank senders, cleaning up payee names, managing users, and re-syncing a date range after a parser fix. |

The interface works the same on a phone and a desktop.

### Banks it reads out of the box

| Bank | Alert emails | Statements (PDF) |
|---|---|---|
| HDFC Bank | UPI debits and credits, credit card purchases, card e-mandates | Savings account, credit card |
| ICICI Bank | Credit card purchases and repayments | Credit card |
| SBI Card | Credit card purchases | Credit card |
| Scapia | Credit card purchases and repayments | Credit card |

Other banks still work: their alerts go through the AI step until Hisaab has learned their format. A weekly
**discovery sweep** looks for financial emails from senders it doesn't know yet, and shows them to the admin
to approve or ignore.

## Privacy and security

- **Your data stays on your computer.** Everything lives in one SQLite file on your own machine. There is no
  Hisaab cloud and no account with anyone but Google (for Gmail) and Anthropic (for the AI step).
- **What does leave the machine:** an email in a format Hisaab doesn't know yet, or a statement in a layout it
  doesn't know, is sent to the Anthropic API to be read. Known formats never leave. As templates are approved,
  less and less is sent.
- **Secrets are encrypted at rest.** Gmail access tokens and account numbers are encrypted with a key kept in
  `.env`, outside the database, so a copied database or backup alone doesn't reveal them. Passwords are stored
  only as Argon2 hashes.
- **Each person sees only their own money.** Every query is scoped to the signed-in user.
- **Use it on a trusted home network only.** The app speaks plain HTTP on your local network so phones can
  reach it. Never forward its port on your router or expose it to the internet. Anyone who can reach it can
  create an account (they see only their own data).

## Getting started

### What you need

- **Windows with WSL2 (Ubuntu), or any Linux machine.** The database must live on the Linux filesystem: in
  WSL, under `/home/...`, never `/mnt/c`.
- **Python 3.14** and **Node.js 24** (inside WSL/Linux, not the Windows versions).
- **A Google Cloud project** with an OAuth client, so Hisaab can read Gmail (step 2 below). It's free.
- **An Anthropic API key**, for emails and statements in formats Hisaab doesn't know yet. Usage is
  pay-as-you-go and small once your banks' templates are in place.

### 1. Install

```bash
git clone https://github.com/PriyanshGupta/hisaab.git
cd hisaab
python3.14 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
```

The frontend is built automatically the first time the app starts.

### 2. Create a Google OAuth client for Gmail

In the [Google Cloud console](https://console.cloud.google.com):

1. Create a project and enable the **Gmail API**.
2. Set up the **OAuth consent screen**:
   - user type **External**, with your own email as the contact;
   - add the scope `https://www.googleapis.com/auth/gmail.readonly`;
   - under *Audience*, choose **Publish app** to set it to **In production**. You don't need to submit it for
     verification, but you do need to publish it: in *Testing* mode, Google expires access after 7 days.
3. Under **Clients**, create an OAuth client of type **Desktop app**. Put its client ID and secret in `.env` as
   `HISAAB_GOOGLE_OAUTH_CLIENT_ID` and `HISAAB_GOOGLE_OAUTH_CLIENT_SECRET`.

Because the app isn't verified by Google, the consent screen shows a warning. Choose *Advanced → Go to …* to
continue. It's your own app, asking for read-only access to your own mail.

### 3. Fill in the rest of `.env`

```bash
# HISAAB_ENCRYPTION_KEY: protects Gmail tokens and account numbers
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# HISAAB_SESSION_SECRET: signs sign-in sessions
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Paste each output into `.env`, and add your Anthropic key as `HISAAB_ANTHROPIC_API_KEY`. The other settings
have working defaults, explained in `.env.example`. Keep `.env` private and out of synced folders. If you lose
the encryption key, you'll have to reconnect Gmail and re-enter account numbers.

### 4. Start it

```bash
scripts/run.sh
```

The script:

1. builds the frontend if needed;
2. backs up the database before any upgrade;
3. applies migrations and loads the built-in bank templates;
4. serves the app on port 8765.

Open **http://localhost:8765**.

To keep it running in the background and have it come back after a reboot, install it as a service:

```bash
scripts/install-service.sh
```

### 5. Create your account and set up

The first visit opens **Create your account**. The first person to sign up becomes the **admin**. Anyone
after that signs up the same way and becomes a regular member, and the admin can manage everyone under
*Admin → Users*.

A guided setup then walks you through:

1. **Your accounts:** each bank account, card and cash wallet you want to track, with the last digits the
   bank's alerts use. An opening balance is optional, but lets Hisaab check your balances later.
2. **Connect Gmail.** Do this step in a browser on the computer running Hisaab: Google sends you back to
   `localhost` when you're done.
3. **History:** Hisaab fetches and reads three months of bank alerts. You can close the page; it carries on.
4. **Categorize:** sort the shops it found into categories. Each answer covers every payment to that shop.

From then on, the Report is your home screen.

### Using it from your phone

Open `http://<your computer's local address>:8765` on any phone on the same Wi-Fi. On Windows, WSL needs one
networking change for this to work. [OPERATIONS.md](OPERATIONS.md) has the steps.

## Running it day to day

[OPERATIONS.md](OPERATIONS.md) covers:

- **Backups:** automatic daily backups, plus one before every upgrade and re-sync, keeping the newest 30.
  Point them at a synced folder if you can.
- **Restoring** a backup.
- **CSV export:** the Report's download, and its columns.
- **The service:** checking on it, reading its log, restarting after an update.
- **Phone access** from WSL.
- **Security:** notes on the network setup.

If every admin ever gets locked out, create a new admin from the terminal:

```bash
.venv/bin/python -m hisaab create-admin --help
```

## For developers

Hisaab is a FastAPI and SQLite (WAL) backend with SQLAlchemy 2.0 and Alembic, and a React 19, TypeScript and
Vite frontend using TanStack Query. The frontend's API client is generated from the backend's OpenAPI schema.
Money is stored as integer paise; there is no floating point anywhere in the accounting path.

```bash
.venv/bin/pytest                                       # backend tests
.venv/bin/mypy                                         # strict type check of src, tests and migrations
cd frontend && npm test                                # frontend tests (regenerates the API client first)
```

Every Python function and variable is type-annotated, and `tests/test_annotations.py` fails on one that
isn't. Design documents and the real bank emails used to write the templates are kept out of this repository
on purpose.

Two rules that prevent real damage:

1. **The database must live on the Linux filesystem** (`/home/...`), never under `/mnt/c`. SQLite locking is
   broken over the Windows mount, and the app refuses to start there.
2. **Secrets live only in `.env`.** Never `export` an API key from a shell profile, rc file or setup script.
   Claude Code would pick up a bare `ANTHROPIC_API_KEY` and bill development work to the app's API credits,
   which is why the app's own variable is `HISAAB_ANTHROPIC_API_KEY`.
