# Relais

**The official bib exchange for running-race organizers.** A runner who can no longer race resells their bib; the buyer is verified; the money stays blocked with a PayPal authorization until the organizer has put the bib in the buyer's name; the service fee is shared between the platform and the organizer.

Built for the PayPal AI Hackathon. **PayPal sandbox only: no real money moves.** The interface is in French; this README gives the English for every label you need to click.

**Live demo:** `https://<your-service>.onrender.com` (to be filled in after deployment)

## The problem

Races sell out months ahead, and some registered runners end up unable to start. Today their bib is either wasted or resold informally, which is bad for everyone:

- **The buyer** pays a stranger and hopes to receive something. Nothing guarantees the bib exists or will be transferred.
- **The seller** has no safe way to get paid.
- **The organizer** ends up with a runner on the course under someone else's name, with no valid medical proof on file. In France a runner needs an FFA licence or a PPS certificate to start, so this is a liability problem as well as a no-show problem.

Most race rules already say what is allowed (transfer or not, until when, at what price), but nothing enforces them.

## What Relais does

1. **The organizer uploads the race rules (PDF).** Claude reads them and extracts five resale rules: transfer allowed, deadline, price cap, transfer fee, accepted proofs. Each value comes with the quoted article it was found in, and a rule the document does not state stays empty instead of being guessed. The organizer reviews and confirms.
2. **A runner lists their bib.** The price is checked against the cap, and the name against the organizer's registry: only the registered holder can sell.
3. **The bib is offered to the first runner on the waitlist**, who has 48 hours to pay. After that the offer expires and goes to the next runner.
4. **The buyer pays with PayPal.** The order uses `intent=AUTHORIZE`: the money is blocked, not taken.
5. **The buyer uploads a proof** (FFA licence or PPS, image or PDF). Claude reads the document type, holder name and validity date. The program then compares them with the buyer's identity and the race date, adds anti-reseller signals computed from the database, and gives a verdict:
   - **green**: the transfer is validated automatically;
   - **orange or red**: the organizer decides from a dashboard, with a one-sentence explanation.
6. **Once validated**, the bib is put in the buyer's name in the registry, the authorization is captured, and PayPal Payouts send the price to the seller and the organizer's share of the fee to the organizer. If the organizer refuses, the authorization is voided and the buyer is never charged.

The buyer pays the bib price plus an 8 % service fee (5 % platform, 3 % organizer), configurable per race.

## Architecture

| Layer | Choice |
|---|---|
| Web | FastAPI, Jinja2 templates, HTMX |
| Database | SQLModel; SQLite locally, Postgres (Neon) in production |
| PayPal, server side | `paypal-server-sdk` for orders, authorizations, captures and voids; `httpx` for Payouts and webhook verification |
| PayPal, browser side | JavaScript SDK v6 |
| AI | Claude through the Anthropic API, with structured outputs |
| Organizer dashboard | AG Grid Community, loaded from a CDN |
| Hosting | Render (see `render.yaml`) |
| Tooling | `uv`, `pytest` |

### A transfer is a state machine

```
EN_VENTE → RESERVE → AUTORISE → VERIFIE → NOM_CHANGE → ENCAISSE → VERSE
on sale    offered   funds      buyer     bib in        captured   seller
                     blocked    verified  buyer's name             paid
```

Two failure states end a transfer: `REFUSE` (authorization voided, buyer never charged) and `OFFRE_EXPIREE` (the buyer did not pay in time; a new transfer is created for the next runner). The transition table is a pure function in `app/transfers/state.py`, and every allowed and forbidden transition has a test.

### Where things live

```
app/
  transfers/      state machine, verification and organizer decisions, post-verification flow
  marketplace/    listings, waitlist, offers and their expiry
  payments/       orders, authorize, capture, void, payouts, ledger, webhook signature check
  ai/             rules_reader.py (PDF → rules with quotes), verifier.py (proof → verdict)
  registry_mock/  simulated organizer registry: list runners, change the name on a bib
  routers/        runner.py, organizer.py, webhooks.py, inbox.py
  templates/, static/
scripts/          seed.py, sandbox_cycle.py (full payment cycle without the UI), make_seed_files.py
seed/             demo races, five sample race rules (PDF), four sample proofs
tests/
```

### How money is handled

- Amounts are integer cents everywhere; the commission split is a pure function.
- Every PayPal call that creates or changes something sends a `PayPal-Request-Id`, so a retry never charges or pays twice.
- Every movement (authorization, capture, void, payouts) writes a line to a ledger table.
- Webhooks are processed only after PayPal confirms their signature, and each event is processed once.
- A background task runs every 5 seconds: it expires unpaid offers, moves verified transfers forward, retries a step PayPal failed, and confirms payouts. It also covers local development, where PayPal cannot reach a webhook.

### How the AI is used

- **Rules reader**: Claude must return every field, with `null` for anything the rules do not say, and the code erases any value returned without a quote.
- **Proof verifier**: Claude only reads the document. It never sees the buyer's name and does not decide the verdict, so a document cannot talk it into approving. The verdict comes from a pure function that can be tested without the AI.
- Proof files are deleted as soon as the analysis is saved, whether it succeeds or fails. Only the verdict and its explanation are stored.

## Run it locally

You need a Mac, Linux or WSL terminal. Nothing else has to be installed beforehand except `uv`, which installs the right Python for you.

**1. Install uv** (skip if `uv --version` works):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**2. Get the code and its dependencies:**

```bash
git clone https://github.com/Antoinechss/Bourse-aux-dossards.git
cd Bourse-aux-dossards
uv sync
```

**3. Create a PayPal sandbox app.** On [developer.paypal.com](https://developer.paypal.com), in *Apps & Credentials* with the *Sandbox* toggle on:

- create an app and note its **Client ID** and **Secret**;
- in the app's features, tick **Payouts**;
- in *Sandbox accounts*, create one **Business** account (the organizer) and two **Personal** accounts (a seller and a buyer). Choose **France** as the country so balances are in euros.

**4. Get an Anthropic API key** from [console.anthropic.com](https://console.anthropic.com). It is needed for the two AI steps (reading rules, checking a proof); the rest of the app works without it.

No key or no credit? Set `AI_MODE=fake` in `.env`. Claude is then not called: the two AI steps return recorded answers for the sample files in `seed/`, refuse any other file, and every page shows a banner saying the AI is simulated.

**5. Create your `.env`:**

```bash
cp .env.example .env
```

| Variable | Value |
|---|---|
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | from step 3 |
| `ANTHROPIC_API_KEY` | from step 4 |
| `AI_MODE` | `real`, or `fake` to run without Claude |
| `DEMO_ORGANIZER_EMAIL` | email of your sandbox Business account |
| `PAYPAL_WEBHOOK_ID` | leave empty locally |
| `DATABASE_URL`, `PAYPAL_BASE_URL`, `APP_ENV` | keep the defaults |

**6. Load the demo races and start the server:**

```bash
uv run python -m scripts.seed
uv run fastapi dev app/main.py
```

Open <http://127.0.0.1:8000>.

**7. Run the tests:**

```bash
uv run pytest            # 390 tests; PayPal and Claude are replaced by fakes
uv run pytest -m live    # 9 more tests that call the real Claude API on the files in seed/ (costs a few cents)
```

To run the suite on Postgres instead of SQLite, point `TEST_DATABASE_URL` at an empty database whose name contains `test` (it is wiped before each test).

To check your PayPal credentials without the interface, `uv run python -m scripts.sandbox_cycle` runs one full cycle (authorize, capture, payouts) and asks you to approve the payment in a browser.

## Try the demo

### Sandbox accounts

| Role | Sandbox account | What it is for |
|---|---|---|
| Buyer | `runner.two@personal.com` | log in with it in the PayPal window to pay |
| Seller | `runner.one@personal.com` | type it as the PayPal email when listing a bib; receives the bib price |
| Organizer | `race.organizer@business.com` | receives the organizer's share of the fee |
| Platform | `platform.manager@business.com` | owns the PayPal app; collects payments and sends payouts |

Passwords: `<to be completed>`

### The main path, in ten minutes

French labels are in quotes.

1. **List a bib.** On the home page, pick *Trail des Crêtes de Valombre* and click "Vendre mon dossard" (sell my bib). Enter bib `101`, first name `Camille`, last name `Roussel`, the seller's PayPal email above, and a price of `45` or less. Try `46` first: the rules cap resale at 45 € and the form says so as you type.
2. **Join the waitlist.** Back on the race page ("Chercher un dossard", find a bib), fill "Rejoindre la liste d'attente" as `Léa` `Moreau` with any email. The bib is offered to her at once.
3. **Open the demo inbox** ("Boîte mail de démo" in the header). Relais sends no real email: every message lands here. Open the one titled "Un dossard est disponible pour vous" and click "Ouvrir dans Relais".
4. **Pay.** Click "Payer le dossard", then the PayPal button, and log in with the buyer sandbox account. The money is authorized, not captured.
5. **Upload the proof.** You are back on the tracking page. Upload `seed/justificatifs/licence-ffa-lea-moreau.png` from this repository and click "Envoyer le justificatif". The licence matches the buyer and is valid on race day, so the expected verdict is green.
6. **Watch it finish.** The page reloads by itself as the transfer advances: bib renamed, payment captured, seller paid. The seller's tracking page (linked from their messages in the inbox) ends on "Vous avez été payé".

Two things to know before you repeat the path:

- Use the seller's real sandbox email in step 1. A payout to an address with no sandbox account stays unclaimed, and the transfer then stops at "Paiement encaissé".
- A second bib listed at the capped price by the same, recently created seller account trips the anti-reseller signal. The transfer then waits for the organizer even if the proof is perfect. List it below the cap to stay on the automatic path.

### The organizer's side

Open "Espace organisateur" (organizer area) in the header.

- **Decide a doubtful transfer.** Repeat the path above with bib `102` (`Théo` `Marchand`), a buyer named `Karim` `Benali`, and the proof `seed/justificatifs/pps-expire-hugo-lemaire.jpg`. The name does not match and the certificate expires before the race, so the expected verdict is red. The transfer appears in the table with the explanation and two buttons: "Valider" (approve) and "Refuser" (refuse, which voids the authorization).
- **Read race rules.** At the bottom of the page, click "Règlement" next to a race and upload `seed/reglements/marathon-deux-rives.pdf`. The review screen shows each rule with its quoted article; this document forbids transfers. Leave with the back link rather than "Confirmer et enregistrer", unless you want to close resale for that race.

Other sample files: `pps-karim-benali.pdf` (valid until 12 May 2027), `carte-salle-de-sport.png` (a gym card, not an accepted proof), and four more race rules in `seed/reglements/`, including one that says nothing about resale.

A bib can only be listed under its holder's name. Each race has its own bib numbers: the sell page lists the registered runners of its race under "Démo : quels dossards puis-je vendre ?" (demo: which bibs can I sell?).

## Deploy

The app runs on Render with a Postgres database on Neon. `render.yaml` describes the service.

1. **Database.** Create a project on [neon.tech](https://neon.tech), in a European region, and copy its connection string (`postgresql://…?sslmode=require`).
2. **Service.** On [render.com](https://render.com), choose *New → Blueprint* and select this repository. Render reads `render.yaml` and asks for the secret values: `DATABASE_URL`, the PayPal Client ID and Secret, `ANTHROPIC_API_KEY`, `DEMO_ORGANIZER_EMAIL`. Enter any placeholder for `PAYPAL_WEBHOOK_ID` for now.
3. **First start.** The start command creates the tables, loads the demo races, then starts the server. Running it again changes nothing: the seed does nothing once races exist.
4. **Webhook.** In the PayPal developer dashboard, open your sandbox app and add a webhook on `https://<your-service>.onrender.com/webhooks/paypal` for the events *Payment capture completed* (`PAYMENT.CAPTURE.COMPLETED`) and *Payment payoutsbatch success* (`PAYMENT.PAYOUTSBATCH.SUCCESS`). Copy the webhook ID into `PAYPAL_WEBHOOK_ID` on Render and redeploy.

Things to know:

- **The free plan sleeps** after 15 minutes without traffic, and takes about a minute to wake up. The background task is paused while it sleeps.
- **There are no migrations.** Tables are created at startup if missing. After a change to an existing table, reset the database (`DROP SCHEMA public CASCADE; CREATE SCHEMA public;` in Neon's SQL editor) and restart the service. The same reset gives you a fresh demo.
- **One instance only.** The background task and its lock assume a single server process.

## Known limits

- Sandbox only, by design: the payment client refuses any other PayPal address.
- No authentication. Tracking pages open by number, the organizer area is open, and the demo inbox shows everyone's messages.
- The verifier reads documents; it does not detect forgeries. For the demo it accepts documents marked "SPÉCIMEN".
- The transfer fee read from the race rules is shown to the organizer but not charged.
- The interface is in French only.

## Licence

MIT.
