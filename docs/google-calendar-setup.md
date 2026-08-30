# Google Calendar backend — setup (Phase 4 scaffold)

The v1 calendar is a local `.ics` file (`data/calendar/main.ics`) that any
calendar app can open. The Google backend syncs the same calendar with your
Google account. It is **inert by default**: until you complete the steps
below and enable it, PLP silently uses the ICS file (a config gap never
takes your calendar down).

Everything stays on your hardware except the OAuth handshake and the
Calendar API calls themselves (Google's servers see your calendar reads/
writes, exactly like any calendar app — PLP never uploads anything else).

## Step 1 — Create a Google Cloud project

1. Go to <https://console.cloud.google.com/> and sign in with the Google
   account whose calendar you want to use.
2. Top bar → project selector → **New Project** → name it `plp` (anything) →
   **Create**.
3. Select the new project in the project selector.

## Step 2 — Enable the Calendar API

1. In the console: **APIs & Services → Library**.
2. Search **Google Calendar API** → **Enable**.

## Step 3 — Configure the OAuth consent screen

1. **APIs & Services → OAuth consent screen**.
2. User Type: **External** → **Create**.
3. App name: `Personal Life Planner`. Email: yours. (Support email optional.)
4. Scopes: remove everything, add **`https://www.googleapis.com/auth/calendar.events`**.
5. Add yourself (your account's email) under **Test users** (required while
   the app is in Testing mode).
6. **Save**.

## Step 4 — Create the OAuth client

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Desktop app**. Name: `plp`.
3. **Download JSON** → save it into this repo at:

   ```
   data/calendar/credentials.json
   ```

   (that path is `calendar.google.credentials_file` in `config/plp.yaml`;
   the file is gitignored, the JSON stays on your machine).

## Step 5 — Connect (one-time, ~30 seconds)

```bash
plp calendar connect
```

A browser tab opens the Google consent page → click **Allow** → the tab
flips to "PLP connected". PLP stores the refresh token + your primary
calendar id back into `data/calendar/credentials.json` and prints the
calendar id.

No browser on this machine? `plp calendar connect --no-browser` prints the
URL; open it anywhere, paste the resulting `code` back with
`--code <code>` (use the same command line the URL was printed for).

## Step 6 — Enable the backend

In `config/plp.yaml`:

```yaml
calendar:
  backend: google
  google:
    enabled: true
    credentials_file: data/calendar/credentials.json
```

Then `plp calendar week` reads from Google; `plp calendar add/rm` and
approved `calendar_block` proposals write to it. Every one of those writes
still goes through the audited `host.calendar_write` path, exactly like the
ICS backend.

## Reverting / re-connecting

- **Back to ICS**: set `enabled: false` (or `backend: ics`). The ICS file is
  untouched; the two backends are independent views of "your calendar".
- **Wrong calendar**: `plp calendar connect --calendar-id <id>` (list ids
  from <https://calendar.google.com/calendar/u/0/r/settings> → "Settings for
  this calendar").
- **Revoke**: Google account → Security → Third-party access → remove
  `plp`; then delete `credentials.json` and re-run step 5.
