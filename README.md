# PR Inbox

A desktop inbox for the GitHub PRs you take part in: it groups notifications by PR and PRs by repo, and shows the action that is up to you.

It shows the PRs that had activity in the last 14 days (adjustable), plus:
- your open PRs;
- PRs with your review pending;
- PRs you reviewed, even if nobody asked you to.

No dependencies: Python 3 (stdlib) plus the system's WebKitGTK. It uses your `gh` accounts or personal tokens stored in the GNOME keyring.

## Install

Requirements: Ubuntu 22.04 or newer with GNOME, and a GitHub account signed in with the GitHub CLI (`gh auth login`) or a personal token.

```bash
./install.sh --check      # only checks the requirements and prints the `sudo apt install …` line for what is missing
./install.sh              # checks, then adds "PR Inbox" to the app menu (then: right click → Pin to Dock)
./install.sh --autostart  # also starts the backend at login (notifications with the window closed)
./install.sh --uninstall
```

The packages it relies on are `python3-gi`, `gir1.2-gtk-3.0` and `gir1.2-webkit2-4.1` for the window, plus these optional ones:
- `gh`, to sign in;
- `gir1.2-secret-1`, for personal tokens;
- `libnotify-bin`, for desktop notifications.

If the app starts with no account, it explains how to connect one and picks up a `gh auth login` on its own.

## Updating

```bash
git pull   # in the PR Inbox folder
```

Then close and reopen the app. With `--autostart`, log out and back in too: the app warns if the backend that is running is an older version. The version is shown in Settings → Advanced.

## Usage

Icons are GitHub's own [Octicons](https://primer.style/octicons) (MIT), embedded in the page.

| Action | When |
|---|---|
| Review (eye) | Your review (or your team's) was requested and you haven't reviewed since the latest commit. Shows how long it has been waiting: amber from 1 day, red from 2 |
| Address changes (pencil) | Your PR has changes requested after your latest commit |
| Fix conflicts (alert) | Your PR has conflicts with the base branch |
| Fix CI (x) | Your open PR has a red pipeline |
| Reply to mention (@) | Someone wrote `@your-user` after your latest activity on the PR |
| Reply to review (conversation) | Your PR has unresolved review conversations whose last comment isn't yours |
| Ask re-approval (sync) | Your PR only has outdated approvals (made on a commit older than the current one) |
| Update branch (branch) | Your PR is ready to merge, but the branch is behind the base and the repo requires updating it |
| Merge (merge) | Your PR is approved, CI is green and it isn't a draft |
| Waiting (hourglass) | Your PR is waiting for reviews or CI |
| FYI (info) | Everything else (merged, closed, activity with no action) |

The Inbox has two blocks:
- **Your turn**: the PRs that need something from you, the same ones the dock counts.
- **Waiting & FYI**: folded by default; the rest.

For each PR (the buttons appear when you point at the row or select it with the keyboard; a toast lets you **Undo** for a few seconds):
- **Done** (check): hides it until there is new activity and marks its notifications as read on GitHub.
- **Snooze** (clock): hides it until the date you choose.
- **Mute** (crossed bell): hides it for good and unsubscribes from the thread on GitHub, so the emails stop.
- **Undo arrow**: in the Snoozed, Done or Muted tabs, moves the PR back to the Inbox.

**Filters** (they combine and are remembered between sessions):
- **Action**, **State**, **Repos** and **Authors**: fixed-width checkbox dropdowns, so nothing moves while you pick. Repos and authors have a search box. With nothing ticked, everything is shown.
- Each filter's counts take the other active filters into account. "Clear filters" removes them all.
- Filters, the active tab and collapsed repos are saved in the local database, so they survive closing the app or rebooting.
- When filters hide PRs, a line under the list says how many, with a link to clear them.

**On each row**, next to the title:
- "conflicts" and "behind": state of the branch against the base (on any open PR).
- Conversation icon + N: unresolved review conversations. Highlighted in orange when any is waiting for your reply.
- +added −deleted · N files: PR size.

**Dock counter**: the icon shows how many Inbox PRs pass your filters (the dropdowns; the search box doesn't count) and need something from you, i.e. all but Waiting and FYI. It updates every 5 s while the app is open. Desktop notifications follow the same filters: PRs they hide don't notify.

**Keyboard shortcuts**: `j`/`k` (or the arrows) to move, `Enter` to expand, `o` to open in the browser, `c` to copy the link, `e` to mark done, `s` to snooze, `/` to search and `Esc` to close.

**Avatars** are highlighted everywhere (author, reviews, comments, detail):
- Blue ring: you.
- Green ring: your GitHub team. In Settings → Appearance you choose which teams count; by default, those with 30 people or fewer.
- Purple ring with an "AI" tag: AI accounts (claude, copilot, depthfirst, coderabbit…).
- "BOT" tag: other bots (dependabot, etc.).

Opening a link shows a discreet notice at the bottom right, "The link opened in the browser".

**Reviews & comments**: one avatar per person, you first. Under each avatar:
- the review state: green check (approved), red diff (changes requested), amber dot (pending);
- a comment bubble if they commented.

A **faded** badge is out of date:
- a faded check or diff is a review made on an older commit;
- a faded bubble means all of their comments came before the latest commit or are on code that has since changed.

The tooltip and the detail spell it out.

**Connection**: the indicator at the top right shows whether GitHub responds.
- Green: connected.
- Amber: more than 3 cycles without syncing.
- Red: GitHub error, expired session (`gh auth login`) or backend down.
- The tooltip shows the last sync and the remaining API quota. Click to sync right away.

The **link** button copies the PR link to the clipboard.

**Theme**: in Settings → Appearance, choose Automatic (follows Ubuntu's light or dark mode, also when you switch it live), Light or Dark.

Clicking a row expands the detail. Empty sections are hidden. It shows, in this order:
1. why the PR has its action;
2. unresolved conversations;
3. the pipeline;
4. reviews and comments;
5. mentions;
6. which notifications brought it in.
- Unresolved review conversations are listed with a link, the file, the last reply and whether they wait for yours.
- The pipeline shows conflicts and whether the branch is behind, plus the PR size.
- Failing checks (red, with their workflow and when they failed) and running (amber dot) or queued (clock, since when) checks are listed with links; passing ones are only counted. When a workflow ran several times on the same commit (for example after editing the title), only its latest run counts, as on GitHub. In the Pipeline column, "Failing" or "Running" links to the first affected check.
- Links open in your browser.

## Settings and multiple accounts

The gear button opens the settings:
- **GitHub accounts**: for each account you see its name, color, whether it is enabled, where the token comes from, the REST and GraphQL API endpoints and the quota left on each.
- **Add account**:
  - **From the GitHub CLI (recommended):** sign in with `gh auth login`, which supports several accounts, then click "Add" in Settings. The app asks `gh` for the token each time and doesn't store it.
  - **With a personal token:** a *classic* token with the `repo`, `read:org` and `notifications` scopes. It is stored in the GNOME keyring. It also works for GitHub Enterprise by setting the server.
- **Several active accounts:**
  - Each PR gets a stripe and a tag in its account color.
  - Repos are grouped by account.
  - The Account filter appears.
  - Desktop notifications show the account.
  - If two accounts see the same PR, it shows once per account.
- **Desktop notifications**: choose which actions notify you (by default the urgent ones: review, changes, conflicts, CI, mentions and review comments) and optional quiet hours, with or without the whole weekend. A PR notifies:
  - when its action changes (for example, your review is requested again). Commits alone don't notify again;
  - when someone comments on it (yours and bot comments don't count). This also applies while the PR is snoozed; a change of action waits until it comes back. Anything arriving during quiet hours isn't notified later, but stays in the Inbox and in the dock counter.
- **Removing an account** deletes its local data and, if it used a personal token, the token in the keyring too.

## Security and privacy

- **What the app does with your GitHub token:** it only reads (notifications, PRs, reviews, CI and your teams). The only changes it makes on GitHub are to your notification threads:
  - **Done** and **Mute** mark them as read;
  - **Mute** also unsubscribes you from the PR, and un-muting subscribes you again.

  It never comments, reviews, merges or touches code.
- **Where the token lives:**
  - with `gh`, the app asks `gh` for it on each run and never stores it;
  - with a personal token, it is kept only in the GNOME keyring.

  The token is only ever sent to the API of the account's own server, over HTTPS.
- **What is stored on disk:** PR titles, comments and CI results from the repos you work on, including private ones. They are kept in `~/.local/share/pr-inbox/`, readable only by your user. Data older than the 14-day window is deleted.
- **Nothing goes to third parties.** The only network traffic is to GitHub (the API and avatar images). There is no telemetry, update check or external script.
- **Content written by others is treated as untrusted.** Anyone who mentions you, even in a public repo, can get a PR into your inbox. Titles, comments and CI names are always shown as plain text, and only http(s) links are opened. The page's security policy only runs the app's own scripts.
- **Several users on one machine:** each one's backend only answers requests carrying their own secret. If the port is already in use by someone else, the window refuses to start instead of connecting to it. Give each user their own `PR_INBOX_PORT`.

## Sync, API quota and limits

Settings → Advanced has two values that set how much GitHub API quota the app spends. Each account has 5,000 GraphQL points per hour, shared with anything else using the same token, such as `gh`.
- **Days of activity** (default 14, from 1 to 60). More days keep more PRs in the list, and each sync has more PRs to check.
- **Sync interval** (default 120 s, from 60 to 3600). Shorter means news arrives sooner and more quota is spent.

Below them, the app shows its estimated hourly spend at that interval, measured on its last syncs.

How a sync works:
- **Notifications** are read with a conditional request, so nothing is spent when there is nothing new.
- **New PRs, and PRs with new notifications,** are always read in full.
- **The other open PRs** are checked with one light query. The app re-reads only the ones whose activity date, latest commit, merge state, review decision or checks changed.
- **Every 10 minutes**, and whenever you click the connection indicator, every PR is read in full. This catches anything the light query can't see, such as a resolved conversation.

Temporary GitHub errors (502, 503, 504) and network blips are retried, and the list keeps the last data meanwhile. They are only reported if they last 3 syncs in a row. Other errors show at once, with what to do, in a banner above the list and in Settings.

Fixed limits, so that each query stays light and doesn't time out:

| What | Up to |
|---|---|
| PRs per search (review requested, authored, reviewed) | 100 |
| Review threads per PR | the latest 40, with 30 comments each |
| Comments per PR | the latest 60 |
| Reviews per PR | the latest 50 |
| Checks on the latest commit | 100 (beyond that, GitHub's overall pipeline state is used) |

These limits are not adjustable on purpose. Raising them makes every PR query heavier, and GitHub then times out more often (the 502 and 504 errors) and charges more points.

## Configuration (environment variables)

- `PR_INBOX_DAYS` (14) and `PR_INBOX_POLL` (120): defaults for the two values above, until you change them in Settings.
- `PR_INBOX_PORT` (8787): local backend port.
- `PR_INBOX_BROWSER=chrome`: use a Chrome app-mode window instead of WebKitGTK.

## Files

- State: `~/.local/share/pr-inbox/state.db`
- Log: `~/.local/state/pr-inbox.log`. Sync failures are logged with their time. Past 1 MB, the oldest lines are dropped and the newest 256 KB are kept.

- Window size and maximized state, restored on the next launch: `~/.local/share/pr-inbox/window.json` (GNOME on Wayland decides the position itself)
- Secret shared by the window and the backend: `~/.local/share/pr-inbox/secret`

These files are only readable by your user. The local server only answers its own window:
- requests from other sites or other host names are rejected;
- every request must carry the secret, so other users of the machine can't read your PRs either.

The secret is created automatically; there is nothing to set up. Opening `http://127.0.0.1:8787` in a normal browser only shows "Open PR Inbox from its launcher".

Debugging and tests:

```bash
python3 server.py --once --dump         # syncs once and prints the classified PRs as JSON
python3 -m unittest discover -s tests   # test suite (no network)
```
