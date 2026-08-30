# Gmail email scanner — setup (Phase 6)

The email scanner reads your Gmail **read-only** (scope
`gmail.readonly` — it can never send, delete, or modify mail). Daily it
triages the last couple of days: flags mail that needs a reply, extracts
dates / RSVP deadlines / birthdays into **calendar proposals** (you approve
them, it never writes the calendar directly), and surfaces life-relevant
mail (wife, family, appointments, gifts).

It is **inert by default**: until you complete the steps below, `plp run
email.scan` logs a note and no-ops. LLM thread summarization is a separate
opt-in (`features.email_summarization` in `config/plp.yaml`, off by
default) and only seasons the report — the deterministic triage ships
regardless.

If you already completed `docs/google-calendar-setup.md`, reuse the same
Google Cloud project — you only need to enable one more API and add one
scope.

## Step 1 — Enable the Gmail API

1. In the [Google Cloud console](https://console.cloud.google.com/)
   (**APIs & Services → Library**), search **Gmail API** → **Enable**.

## Step 2 — Add the read-only scope

1. **APIs & Services → OAuth consent screen** → edit.
2. Under **Scopes**, add **`https://www.googleapis.com/auth/gmail.readonly`**
   (leave the calendar scope alone if you have it).
3. **Save**.

## Step 3 — Re-download the OAuth client JSON

1. **APIs & Services → Credentials** → your `plp` OAuth client ID
   (desktop app) → **Edit** → make sure **Authorized redirect URIs**
   include `http://127.0.0.1` (a bare host is fine — PLP picks the port).
2. **Download JSON** → save it into this repo at:

   ```
   data/email/google_credentials.json
   ```

   (that path is `email.credentials_file` in `config/plp.yaml`; the file is
   gitignored, the JSON stays on your machine).

## Step 4 — Connect (one-time, ~30 seconds)

```bash
plp email connect
```

A browser tab opens the Google consent page → click **Allow** → the tab
flips to "PLP email connected". PLP stores the refresh token in
`data/email/token.json` (gitignored; auto-refreshed on every scan, never
uploaded anywhere but Google's token endpoint).

No browser on this machine? `plp email connect --no-browser` prints the
URL; open it anywhere and the redirect lands on `127.0.0.1` — if the
redirect can't reach this box, copy the `code=...` value out of the final
URL and note that you'll need to re-run with the code pasted.

## Step 5 — Enable the daily scan

In `config/plp.yaml`:

```yaml
email:
  credentials_file: data/email/google_credentials.json
  scan_days: 2            # how far back each scan looks
  scan_cron: "0 7 * * *"  # daily, with the news collect
```

Then:

```bash
plp run email.scan      # immediate triage + proposals
plp email recent        # raw peek at the last days of mail
plp approvals pending   # review the calendar proposals it filed
```

Every proposed date lands in the approvals queue exactly like the checkup's
proposals — nothing touches your calendar until you `plp approve <id>`.
