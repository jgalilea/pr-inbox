# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

PR Inbox is a desktop app for Ubuntu GNOME. It groups the user's GitHub PR notifications by PR and by repo, and works out the action that is up to them. User-facing behaviour is documented in `README.md`; keep the README in sync when features change. All UI and backend text is in English.

## Constraints

- **No dependencies.** The backend uses only the Python 3 stdlib. The window uses the system's PyGObject, Gtk 3 and WebKit2 4.1. The frontend is a single `static/index.html` with inline JS and CSS, no build step and no frameworks. Don't add pip packages, npm or bundlers.
- **Tokens are never written to app files or the SQLite DB.**
  - `gh`-sourced tokens are fetched from `gh auth token` on demand (`accounts.py`).
  - Personal access tokens live only in the GNOME keyring (via `secret-tool`).
- **Git:** the repo is public at `jgalilea/pr-inbox`, with `main` as the default branch. `.github/workflows/tests.yml` runs the tests in CI. There is no linter; tests are stdlib `unittest` (see below).
- **Version:** `VERSION` in `server.py` is shown in Settings, and `/api/ping` returns it. `app.py` warns if the running backend is a different version, which happens when an autostart backend runs old code. Bump it on user-visible changes.
- **Local API hardening (keep it when adding endpoints):**
  - every request must carry `Host` 127.0.0.1/localhost:<port> and, if present, a matching `Origin` (blocks DNS rebinding and cross-site requests);
  - every request except `/api/ping` must carry the shared secret (`$DATA_DIR/secret`, 0600, created by `secret_token()`), so other local users can't use the API:
    - the window opens `/auth?k=<secret>`, which sets an HttpOnly, SameSite=Strict cookie named per port and redirects to `/`;
    - `app.py` sends the `X-PR-Inbox-Token` header when it polls the badge;
    - Chrome mode goes through a private `open.html` redirect, so the secret never appears in `ps`;
  - `/api/ping?n=<nonce>` returns `proof` = HMAC-SHA256(secret, nonce). `app.py` checks it before trusting a backend on the port; if the check fails it exits instead of handing the secret to a squatter or another user's backend;
  - POSTs must be JSON objects of at most 1 MB;
  - HTML responses carry a strict CSP: no third-party scripts, styles or connections.
    - Scripts run only with a per-response nonce. The server adds it to every `<script>` tag of `.html` files, so don't use inline event handlers (`onclick=`…) or `javascript:` URLs.
    - The UI must `esc()` everything from GitHub: PR titles, comments and CI names can be written by anyone who mentions the user.
- **`notify-send`** gets `--` before its positional arguments, since PR titles can start with `-`.
- **Links:** every URL that reaches the UI goes through `safe_url()` (http/https only), because CI `targetUrl`s are set by third parties. `app.py` only opens http(s) links externally.
- **Token routing:** `github.py` refuses to send the token to any URL outside the account's own API base.
- **Permissions:** the data directory is 0700 and the DB and log are 0600.

## Running and checking changes

```bash
python3 -m unittest discover -s tests                                   # all tests (no network, temp DB)
python3 -m unittest tests.test_server.ClassifyTest.test_draft_waits      # a single test
python3 server.py --once --dump        # sync once, print the classified state as JSON (main debugging tool)
python3 server.py                      # backend only, on 127.0.0.1:8787
./pr-inbox                             # window (starts the backend in-process if none is running); logs to ~/.local/state/pr-inbox.log
./install.sh [--check|--autostart|--uninstall] # requirement check (apt packages, gh login) + .desktop entry / backend at login
```

To test without touching the user's real instance, run a second backend on another port with its own data directory:

```bash
PR_INBOX_PORT=8799 XDG_DATA_HOME=<scratch>/data python3 server.py
```

Then open or screenshot `http://127.0.0.1:8799/auth?k=$(cat <scratch>/data/pr-inbox/secret)` (for example with headless Chrome). Without the secret you only get a "locked" page. With curl, send `-H "X-PR-Inbox-Token: …"`.
- Static files other than the index are served under `/static/<name>`, not at the root.
- UI prefs (filters, tab) are persisted server-side, so screenshots may show filtered views.
- Stop the test server by port (`ss -ltnp | grep ':8799 '`, then kill that PID). Never use `pkill -f server.py`: it matches the running shell too.
- Changes only reach the user's app after they close and reopen it.

Environment variables:
- `PR_INBOX_DAYS` (default 14) and `PR_INBOX_POLL` (default 120 s): defaults for the window and the sync interval. Settings → Advanced overrides them (stored in `meta`, limits in `SYNC_LIMITS`). `apply_sync_settings()` sets the module globals `DAYS` and `POLL_SECONDS`;
- `PR_INBOX_PORT` (default 8787): backend port;
- `PR_INBOX_BROWSER=chrome`: use a Chrome `--app` window instead of WebKitGTK.

## Architecture

- **`app.py`** is the window: a single-instance `Gtk.Application` with app ID `com.github.javiergalilea.PrInbox`. This ID must match the `.desktop` file name and `StartupWMClass` for the dock icon to work.
  - The primary instance calls `server.serve()` in-process, unless a backend already answers `/api/ping` (e.g. the autostart one).
  - Non-app URLs are sent to the default browser.
  - The GNOME color-scheme is mirrored into GTK so `prefers-color-scheme` works.
  - Pin every `gi` namespace with `gi.require_version` before importing it (Gtk, Gdk 3.0, WebKit2 4.1). An unpinned `Gdk` loads 4.0, WebKit then fails, and the app silently falls back to Chrome mode; the log says `WebKitGTK unavailable`.
  - `remember_geometry()` saves the size and maximized state to `$DATA_DIR/window.json` on close. The position is only restored on X11, because Wayland ignores `move()`.
  - It polls `/api/badge` every 5 s and emits the Unity `LauncherEntry` DBus signal for the dock counter.
- **`server.py`** holds the backend: the `Store`, `Account`, `Inbox` and `classify` pieces and the HTTP handler.
  - **`Store`:** SQLite at `$XDG_DATA_HOME/pr-inbox/state.db` (default `~/.local/share/pr-inbox/state.db`). Tables:
    - `prs`: cached GraphQL PR node as JSON;
    - `threads`: notification threads;
    - `marks`: done, snooze or mute;
    - `meta`: key/value, including `ui_prefs` and settings;
    - `accounts`.
    - Schema migrations are done inline in `Store.__init__`.
  - **`Account`:** one per GitHub account. Each one owns a `github.GitHub` client.
    - Every key is `"<account_id>|owner/repo#123"`, so the same PR seen by two accounts appears twice.
    - Per-account metadata is stored in `meta` under keys prefixed with the account.
    - `_sync()` does the following:
      1. fetches REST notifications (`participating=true`, using `If-Modified-Since`, so a 304 costs no quota);
      2. runs three GraphQL searches, whose keys go to `search_keys`:
         - open PRs where the user's review is requested;
         - open PRs they authored;
         - PRs they reviewed and that were updated within the window. GitHub sends no notification for your own review, so this is the only way to see those.
      3. re-fetches PR details in aliased GraphQL batches using `PR_FRAGMENT`:
         - new PRs and PRs with new notifications, always;
         - open or searched PRs, only if `fingerprint()` differs between the cached node and a cheap `LIGHT_FRAGMENT` query, except every `FULL_REFRESH` (10 min) or after `/api/sync`, when they are all re-read.

         `fingerprint()` must read only fields that both fragments have. If you add a field that should trigger a re-read, add it to `LIGHT_FRAGMENT` and `fingerprint()` together.
    - `sync()` measures the GraphQL points each light or full sync costs (`cost`), for the quota estimate in Settings.
    - Errors from `GitHubError(transient=True)`, which are 5xx responses, network errors or an exhausted quota, are logged but only set `error` (what the UI shows) after `TRANSIENT_GRACE` failures in a row. `last_failure` always holds the latest one.
    - `prune()` runs after each sync. It drops threads older than the window, and cached PRs and done/snooze marks that are no longer shown. Mute marks are kept.
    - Closed or merged PRs keep their cached node until they get new activity, so code that reads new fields must tolerate older cached nodes where the field is missing.
  - **`Inbox`:** manages the list of accounts and runs the sync loop (woken early by `/api/sync`).
    - With no account, it starts empty; it doesn't exit. Each sync retries `_bootstrap_account()` from `gh`, and the UI shows the onboarding steps.
    - The loop calls `trim_log()` on every cycle: past 1 MB it keeps the newest 256 KB.
    - `items()` joins PRs, threads and marks into tabs (inbox, snoozed, done, muted).
    - `state()` is the single payload the UI renders.
    - It also owns marks, settings (notification actions, quiet hours), `badge()` and `desktop_notify()` (via `notify-send`).
    - `desktop_notify()` keeps a per-PR log of `{action, comment}` in `notified_state`. It notifies:
      - on a change of action (pushes alone don't notify);
      - on a newer `last_comment`, which comes from a person: not the user, not a bot.

      PRs hidden by the UI filter dropdowns (`Inbox.visible()`, shared with `badge()`) don't notify, but are still logged. Snoozed PRs notify comments only. Done and muted PRs don't notify. The first run is silent. `"comment"` is a notification kind in `NOTIFY_LABELS` but not an action.
  - **`classify(key, pr, threads, me)`** is the core logic. It turns one PR node into the row model:
    - the action;
    - reviews, with stale approvals made on a commit older than the head;
    - CI, made of checks deduplicated by (workflow, name), keeping the latest run (highest `runNumber`, then `startedAt`), and split into failing, running and passed. The pipeline state comes from these checks. `statusCheckRollup.state` counts superseded reruns too, so it is only a fallback when there are no checks or the contexts list is truncated (`totalCount`);
    - open review threads, conflicts, behind-base state, size and review-waiting age.

    The action is the first that applies, in `ACTIONS` priority order: review, changes, conflicts, ci, mention, threads, reapproval, update, merge, waiting, fyi. `_explain()` produces the human "why" text for each action.

    When you add an action, update all of these together:
    - `ACTIONS` and, if it applies, `NOTIFY_LABELS` and `BADGE_ACTIONS` in `server.py`;
    - the action metadata in `index.html`;
    - the README table.
  - **HTTP:**
    - GET `/api/state`, `/api/badge`, `/api/prefs`, `/api/ping`, `/api/accounts/available`.
    - POST `/api/mark`, `/api/unmark`, `/api/sync`, `/api/settings`, `/api/prefs`, `/api/accounts/*`.
    - POSTs must be `application/json`, to block cross-site form posts. Most POSTs return the full `state()`.
- **`github.py`** is a minimal `urllib` REST and GraphQL client.
  - It tracks rate limits from response headers.
  - On a 401 it retries once with a re-read token, which picks up `gh auth refresh`.
  - It retries 502, 503 and 504 responses and network errors twice (`RETRY_DELAYS`).
  - It raises `GitHubError` with a user-facing message that says what to do, never raw HTML. `accounts.py` does the same for problems with `gh` or the keyring.
  - It works with GitHub Enterprise hosts.
  - New PR data is added to `PR_FRAGMENT`.
- **`accounts.py`** wraps the `gh` CLI (listing accounts, getting tokens) and the GNOME keyring (`secret-tool`).
- **`static/index.html`** holds the whole UI:
  - tabs, the multi-select filters (persisted via `/api/prefs`), rows grouped by repo and expandable details;
  - the settings drawer and the keyboard shortcuts;
  - a light/dark/auto theme driven by CSS variables.
  - Icons are inline Octicons (MIT) in `ICONS`, rendered with `ic(name, cls)`. To add one, copy its 16px path from `@primer/octicons` `build/data.json`. Don't use emojis in the UI. The classes `ok`, `bad`, `wait` and `mut` give the status colors.
  - The detail is a wrapping flexbox: "Reviews & comments" grows (`flex: 3`) so each person fits on one line.

  - The Inbox is split into "Your turn" (actions outside `LATER`, matching `BADGE_ACTIONS`) and a folded "Waiting & FYI".
  - The Reviews column merges `reviewers` and `commenters` per person (`people()`). Faded badges mean a stale review, or a commenter with `outdated` set.
  - Row buttons are CSS-hidden until hover or selection.
  - `markPR()` shows an Undo toast.
  - The theme and team highlighting live in Settings → Appearance.

  It polls `/api/state` and re-renders. Dates and numbers use the `en-GB` locale.
