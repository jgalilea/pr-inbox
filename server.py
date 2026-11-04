#!/usr/bin/env python3
"""PR Inbox backend: syncs GitHub PR notifications, classifies them and serves the UI.

Usage:
  server.py                 run the HTTP server + background sync (daemon mode)
  server.py --once --dump   sync once and print the computed items as JSON
"""
import argparse
import hmac
import html
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

import accounts as acc
from github import LIGHT_FRAGMENT, GitHub, endpoints

VERSION = "1.0.2"
HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "pr-inbox"
PORT = int(os.environ.get("PR_INBOX_PORT", 8787))
LOG_FILE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "pr-inbox.log"
# Defaults for the window and the interval; Settings → Advanced can override them (see apply_sync_settings).
ENV_DAYS = int(os.environ.get("PR_INBOX_DAYS", 14))
ENV_POLL = int(os.environ.get("PR_INBOX_POLL", 120))
SYNC_LIMITS = {"days": (1, 60), "poll": (60, 3600)}  # GitHub asks not to poll notifications more often than 60 s
DAYS, POLL_SECONDS = ENV_DAYS, ENV_POLL
FULL_REFRESH = 600  # seconds between full re-reads of every tracked PR; in between, only the ones that changed
TRANSIENT_GRACE = 3  # consecutive temporary failures (GitHub 5xx, network) before the UI reports an error

# Action -> (priority, notify on desktop). Lower priority sorts first.
ACTIONS = {
    "review": (0, True),
    "changes": (1, True),
    "conflicts": (2, True),
    "ci": (3, True),
    "mention": (4, True),
    "threads": (5, True),
    "reapproval": (6, False),
    "update": (7, False),
    "merge": (8, False),
    "waiting": (9, False),
    "fyi": (10, False),
}
# Actions that count towards the dock badge (something for you to do).
BADGE_ACTIONS = {a for a in ACTIONS if a not in ("waiting", "fyi")}
# Desktop notification kinds: every action except waiting/fyi, plus new comments.
NOTIFY_LABELS = {"comment": "New comment", "review": "Review requested", "changes": "Changes requested", "conflicts": "Merge conflicts",
                 "ci": "CI broken", "mention": "You were mentioned", "threads": "Unanswered review comments",
                 "reapproval": "Re-approval needed", "update": "Branch behind", "merge": "Ready to merge"}
DEFAULT_QUIET = {"enabled": False, "from": "19:00", "to": "09:00", "weekends": True}
ACCOUNT_COLORS = ["#6366f1", "#db2777", "#0d9488", "#ea580c", "#a16207", "#0284c7", "#65a30d", "#9333ea"]
# Per-account meta keys (stored as "<key>:<account id>").
ACCOUNT_META = ("last_modified", "search_keys", "review_keys", "notified", "teams", "teams_at", "me", "last_sync", "gh_user")
FAILED_CHECK = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR"}
RUNNING_CHECK = {"IN_PROGRESS", "QUEUED", "PENDING", "EXPECTED", "WAITING", "REQUESTED"}
HOST_RE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:\d{1,5})?")
LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,38}")
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
MAX_BODY = 1 << 20


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def secret_token():
    """Shared secret between the window and the backend, so other local users can't use the API.
    Created on first use in the private data directory; the window reads the same file."""
    path = DATA_DIR / "secret"
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_urlsafe(32))
    return path.read_text().strip()


def auth_cookie(port):
    return f"pr_inbox_{port}"  # cookies are shared by every port of 127.0.0.1


def safe_url(url):
    """Only http(s) links reach the UI: CI status targetUrls are set by third parties (no javascript:/file: links)."""
    return url if isinstance(url, str) and re.match(r"https?://", url, re.I) else None


def apply_sync_settings(store):
    global DAYS, POLL_SECONDS
    DAYS, POLL_SECONDS = store.get("days", ENV_DAYS), store.get("poll", ENV_POLL)


def trim_log(path=None, max_bytes=1 << 20, keep=256 << 10):
    """Past max_bytes, drop the oldest lines and keep the newest `keep` bytes (writers append, so they carry on)."""
    path = path or LOG_FILE
    try:
        if path.stat().st_size <= max_bytes:
            return False
        with open(path, "r+b") as f:
            f.seek(-keep, os.SEEK_END)
            tail = f.read()
            tail = tail[tail.find(b"\n") + 1:]
            f.seek(0)
            f.write(f"{now_iso()} [log trimmed: older lines removed]\n".encode() + tail)
            f.truncate()
        return True
    except OSError:
        return False


def fingerprint(node):
    """The fields LIGHT_FRAGMENT reads, taken from a light or a full PR node: equal means nothing to re-read."""
    if not node:
        return None
    commit = ((((node.get("commits") or {}).get("nodes") or [{}])[0] or {}).get("commit") or {})
    rollup = commit.get("statusCheckRollup") or {}
    ctx = rollup.get("contexts") or {}
    return ([node.get(k) for k in ("updatedAt", "headRefOid", "state", "isDraft", "mergeable", "mergeStateStatus", "reviewDecision")]
            + [rollup.get("state"), ctx.get("totalCount"),
               [[c.get("status"), c.get("conclusion")] if c.get("__typename") == "CheckRun" else [c.get("state")]
                for c in ctx.get("nodes") or []]])


def like_prefix(prefix):
    """SQL LIKE pattern matching strings that start with `prefix` literally (logins can contain "_")."""
    return re.sub(r"([\\%_])", r"\\\1", prefix) + "%"


# --- storage -------------------------------------------------------------


class Store:
    def __init__(self, path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)  # PR data from private repos: keep it to this user
        path.touch(mode=0o600)
        path.chmod(0o600)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS prs (key TEXT PRIMARY KEY, data TEXT, fetched_at TEXT);
            CREATE TABLE IF NOT EXISTS threads (id TEXT, pr_key TEXT, reason TEXT,
                unread INTEGER, updated_at TEXT, repo TEXT, title TEXT, account TEXT, PRIMARY KEY (account, id));
            CREATE TABLE IF NOT EXISTS marks (pr_key TEXT PRIMARY KEY, kind TEXT, at TEXT,
                until TEXT, action TEXT);
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, source TEXT, host TEXT, login TEXT,
                label TEXT, color TEXT, enabled INTEGER DEFAULT 1, pos INTEGER);
        """)
        cols = {r["name"]: r["pk"] for r in self.q("PRAGMA table_info(threads)")}
        if cols.get("account", 0) == 0:  # pre multi-account schema: thread ids are only unique per account
            with self.lock:
                self.db.executescript(f"""
                    ALTER TABLE threads RENAME TO threads_old;
                    CREATE TABLE threads (id TEXT, pr_key TEXT, reason TEXT, unread INTEGER, updated_at TEXT,
                        repo TEXT, title TEXT, account TEXT, PRIMARY KEY (account, id));
                    INSERT OR REPLACE INTO threads SELECT id, pr_key, reason, unread, updated_at, repo, title,
                        {"account" if "account" in cols else "NULL"} FROM threads_old;
                    DROP TABLE threads_old;
                """)

    def migrate_single_account(self, account_id):
        """Data from before multi-account support belongs to the first account: prefix its keys."""
        if not self.q("SELECT 1 FROM threads WHERE account IS NULL LIMIT 1") and not self.q(
                "SELECT 1 FROM prs WHERE key NOT LIKE '%|%' LIMIT 1"):
            return
        pre = account_id + "|"
        with self.lock:
            self.db.execute("UPDATE threads SET account=?, pr_key=?||pr_key WHERE account IS NULL", (account_id, pre))
            self.db.execute("UPDATE prs SET key=?||key WHERE key NOT LIKE '%|%'", (pre,))
            self.db.execute("UPDATE marks SET pr_key=?||pr_key WHERE pr_key NOT LIKE '%|%'", (pre,))
            self.db.commit()
        for k in ACCOUNT_META:
            v = self.get(k)
            if v is None:
                continue
            if k in ("search_keys", "review_keys", "notified"):
                v = [pre + x for x in v]
            if k == "notified":  # desktop-notification log stays global (keys now carry the account)
                self.set(k, v)
                continue
            self.set(f"{k}:{account_id}", v)
            self.x("DELETE FROM meta WHERE k=?", k)

    def q(self, sql, *args):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql, *args):
        with self.lock:
            self.db.execute(sql, args)
            self.db.commit()

    def get(self, k, default=None):
        rows = self.q("SELECT v FROM meta WHERE k=?", k)
        return json.loads(rows[0]["v"]) if rows else default

    def set(self, k, v):
        self.x("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", k, json.dumps(v))


# --- classification ------------------------------------------------------


def classify(key, pr, threads, me, review_search=False):
    """Build the UI item for one PR from its GraphQL node and its notification threads."""
    me_l = me.lower()
    mention_re = re.compile(rf"(?<![\w/@-])@{re.escape(me)}\b", re.I)
    login = lambda node: ((node or {}).get("author") or {}).get("login") or "ghost"
    avatar = lambda node: safe_url(((node or {}).get("author") or {}).get("avatarUrl"))

    author = login(pr)
    mine = author.lower() == me_l
    state = pr["state"]
    if state == "OPEN" and pr["isDraft"]:
        state = "DRAFT"
    is_open = pr["state"] == "OPEN"

    commit = (pr["commits"]["nodes"] or [{}])[0].get("commit") or {}
    last_commit = parse_ts(commit.get("committedDate"))
    head = pr.get("headRefOid")

    def stale(review):
        """A review is outdated when it was made on a commit that is no longer the PR head."""
        if not review or review.get("state") not in ("APPROVED", "CHANGES_REQUESTED"):
            return False
        oid = (review.get("commit") or {}).get("oid")
        if head and oid:
            return oid != head
        return bool(last_commit and parse_ts(review["submittedAt"]) < last_commit)

    # Reviewers: latest review per person, plus pending requests.
    reviewers = {}
    my_review = None
    for r in pr["latestReviews"]["nodes"]:
        who = login(r)
        if who.lower() == me_l:
            my_review = r
        if who != author:
            reviewers[who] = {"login": who, "avatar": avatar(r), "state": r["state"],
                              "at": r["submittedAt"], "url": safe_url(r["url"]), "stale": stale(r)}
    requested_me = False
    requested_teams = []
    for rr in pr["reviewRequests"]["nodes"]:
        rev = rr["requestedReviewer"] or {}
        if rev.get("__typename") in ("User", "Bot"):
            who = rev["login"]
            requested_me |= who.lower() == me_l
            prev = reviewers.get(who, {})
            reviewers[who] = {"login": who, "avatar": safe_url(rev.get("avatarUrl")), "state": "PENDING",
                              "at": prev.get("at"), "url": prev.get("url"), "prev": prev.get("state"),
                              "stale": False, "rerequested": bool(prev)}
        elif rev.get("__typename") == "Team":
            requested_teams.append(rev["slug"])
            reviewers["team:" + rev["slug"]] = {"login": rev["slug"], "team": True, "state": "PENDING", "stale": False}

    # Every piece of written activity: PR body, comments, review bodies, review-thread comments.
    entries = [{**pr, "createdAt": pr["createdAt"], "url": pr["url"], "kind": "body"}]
    entries += [{**c, "kind": "comment"} for c in pr["comments"]["nodes"]]
    entries += [{**r, "kind": "review"} for r in pr["reviews"]["nodes"]]
    entries += [{**c, "kind": "review_comment", "thread_outdated": bool(t.get("isOutdated"))}
                for t in pr["reviewThreads"]["nodes"] for c in t["comments"]["nodes"]]

    # Accounts GitHub reports as bots (apps such as claude, copilot, dependabot).
    bots = {login(n) for n in entries + pr["latestReviews"]["nodes"] + [pr]
            if ((n or {}).get("author") or {}).get("__typename") == "Bot"}
    bots |= {(rr["requestedReviewer"] or {}).get("login") for rr in pr["reviewRequests"]["nodes"]
             if (rr["requestedReviewer"] or {}).get("__typename") == "Bot"}
    bots.discard(None)

    commenters, mentions, my_last, last_comment = {}, [], None, None
    for e in entries:
        who = login(e)
        ts = parse_ts(e["createdAt"])
        if who.lower() == me_l:
            if e["kind"] != "body" or mine:
                my_last = max(filter(None, [my_last, ts]))
            continue
        if e["kind"] != "body" and (e.get("body") or "").strip():
            c = commenters.setdefault(who, {"login": who, "avatar": avatar(e), "count": 0, "last": None, "outdated": True})
            c["count"] += 1
            c["last"] = max(filter(None, [c["last"], e["createdAt"]]))
            # Outdated: written before the latest commit or on code that has since changed. One fresh comment clears it.
            if not (e.get("thread_outdated") or (last_commit and ts < last_commit)):
                c["outdated"] = False
            # Latest comment from a person (bots such as CI reporters comment on every push): drives notifications.
            if who not in bots and (not last_comment or e["createdAt"] > last_comment["at"]):
                last_comment = {"by": who, "at": e["createdAt"], "url": safe_url(e["url"]),
                                "excerpt": _excerpt(e["body"], None, 120)}
        if mention_re.search(e.get("body") or ""):
            mentions.append({"by": who, "avatar": avatar(e), "at": e["createdAt"], "url": safe_url(e["url"]),
                             "excerpt": _excerpt(e["body"], mention_re)})
    mentions.sort(key=lambda m: m["at"])
    for m in mentions:
        m["answered"] = bool(my_last and my_last > parse_ts(m["at"]))
    pending_mentions = [m for m in mentions if not m["answered"]]

    # CI: one entry per check, its latest run only (like the PR page). Reruns of a workflow on the same commit
    # (e.g. after editing the title) keep the highest run number; started time and list order break ties.
    rollup = commit.get("statusCheckRollup") or {}
    contexts = rollup.get("contexts") or {}
    checks, newest = {}, {}
    for n, c in enumerate(contexts.get("nodes") or []):
        if c["__typename"] == "CheckRun":
            result = c["conclusion"] or c["status"]
            wf_run = (c.get("checkSuite") or {}).get("workflowRun") or {}
            workflow = (wf_run.get("workflow") or {}).get("name")
            ck, order = (workflow, c["name"]), (wf_run.get("runNumber") or 0, c.get("startedAt") or "", n)
            check = {"name": c["name"], "workflow": workflow, "result": result, "url": safe_url(c["detailsUrl"]),
                     "started": c.get("startedAt"), "finished": c.get("completedAt")}
        else:
            ck, order = (None, c["context"]), (0, "", n)
            check = {"name": c["context"], "workflow": None, "result": c["state"],
                     "url": safe_url(c["targetUrl"]), "started": None, "finished": None}
        if ck not in newest or order > newest[ck]:  # ck, not key: that is the PR's key
            checks[ck], newest[ck] = check, order
    failing = [c for c in checks.values() if c["result"] in FAILED_CHECK]
    running = [c for c in checks.values() if c["result"] in RUNNING_CHECK]
    passed = sum(c["result"] == "SUCCESS" for c in checks.values())
    # GitHub's rollup state counts every run, so a superseded failed rerun keeps it red: derive the state from the
    # latest runs instead. Only when we didn't get every check is the rollup the safer answer.
    if not checks or (contexts.get("totalCount") or 0) > len(contexts.get("nodes") or []):
        ci_state = rollup.get("state")
    else:
        ci_state = "FAILURE" if failing else "PENDING" if running else "SUCCESS"

    reasons = sorted({t["reason"] for t in threads})
    if not requested_me and requested_teams and ("review_requested" in reasons or review_search):
        requested_me = not my_review or stale(my_review)

    # When the review was asked of you (or your team); after a stale review of yours, the new commits restart the clock.
    asked = [e["createdAt"] for e in ((pr.get("timelineItems") or {}).get("nodes") or [])
             if e and (((e.get("requestedReviewer") or {}).get("login") or "").lower() == me_l
                       or (e.get("requestedReviewer") or {}).get("slug") in requested_teams)]
    review_since = max(asked) if asked else None
    if my_review and stale(my_review) and commit.get("committedDate"):
        review_since = max(filter(None, [review_since, commit["committedDate"]]))

    # Unresolved review conversations; on your PRs, the ones whose last word isn't yours are waiting for you.
    open_threads = []
    for t in pr["reviewThreads"]["nodes"] if is_open else []:
        cs = t["comments"]["nodes"]
        if t.get("isResolved", True) or not cs:
            continue
        first, last = cs[0], cs[-1]
        open_threads.append({"path": t.get("path"), "outdated": bool(t.get("isOutdated")), "url": safe_url(first["url"]),
                             "by": login(first), "avatar": avatar(first), "at": first["createdAt"],
                             "excerpt": _excerpt(first.get("body") or "", None, 140),
                             "last_by": login(last), "last_at": last["createdAt"], "replies": len(cs) - 1,
                             "waiting": mine and login(last).lower() != me_l})
    threads_waiting = [t for t in open_threads if t["waiting"]]

    conflicts = is_open and pr.get("mergeable") == "CONFLICTING"
    behind = is_open and pr.get("mergeStateStatus") == "BEHIND"

    # Changes requested only count while nobody has pushed after them.
    blocking = [r for r in reviewers.values() if r["state"] == "CHANGES_REQUESTED"]
    changes_pending = pr["reviewDecision"] == "CHANGES_REQUESTED" and any(not r["stale"] for r in blocking)
    fresh_approvals = [r for r in reviewers.values() if r["state"] == "APPROVED" and not r["stale"]]
    stale_approvals = [r for r in reviewers.values() if r["state"] == "APPROVED" and r["stale"]]
    approved = pr["reviewDecision"] == "APPROVED" or (pr["reviewDecision"] is None and fresh_approvals)
    needs_reapproval = is_open and bool(stale_approvals) and not fresh_approvals

    if is_open and not mine and requested_me:
        action = "review"
    elif is_open and mine and changes_pending:
        action = "changes"
    elif is_open and mine and conflicts:
        action = "conflicts"
    elif is_open and mine and ci_state in ("FAILURE", "ERROR"):
        action = "ci"
    elif pending_mentions:
        action = "mention"
    elif is_open and mine and threads_waiting:
        action = "threads"
    elif mine and needs_reapproval:
        action = "reapproval"
    elif is_open and mine and not pr["isDraft"] and approved and ci_state in ("SUCCESS", None):
        action = "update" if behind else "merge"
    elif is_open and mine:
        action = "waiting"
    else:
        action = "fyi"

    why, role = _explain(action, locals()), _role(locals())

    thread_times = [t["updated_at"] for t in threads]
    activity = max(thread_times) if thread_times else pr["updatedAt"]
    return {
        "key": key, "repo": pr["repository"]["nameWithOwner"], "number": pr["number"], "title": pr["title"], "url": safe_url(pr["url"]),
        "state": state, "author": {"login": author, "avatar": avatar(pr)}, "mine": mine,
        "review_decision": pr["reviewDecision"],
        # You first, then people who already reviewed, then pending requests.
        "reviewers": sorted(reviewers.values(), key=lambda r: (
            r["login"].lower() != me_l, r["state"] == "PENDING", r["login"].lower())),
        "stale_approvals": len(stale_approvals), "fresh_approvals": len(fresh_approvals),
        "needs_reapproval": needs_reapproval, "bots": sorted(bots), "why": why, "role": role,
        "commenters": sorted(commenters.values(), key=lambda c: c["last"], reverse=True),
        "mentions": mentions, "pending_mentions": len(pending_mentions), "last_comment": last_comment,
        "ci": {"state": ci_state, "failing": failing, "running": running, "passed": passed,
               "total": len(checks)},
        "conflicts": conflicts, "behind": behind, "review_since": review_since if action == "review" else None,
        "open_threads": sorted(open_threads, key=lambda t: (not t["waiting"], t["at"])),
        "threads_waiting": len(threads_waiting),
        "size": {"additions": pr.get("additions"), "deletions": pr.get("deletions"), "files": pr.get("changedFiles")},
        "action": action, "priority": ACTIONS[action][0],
        "reasons": reasons, "unread": any(t["unread"] for t in threads),
        "threads": sorted(threads, key=lambda t: t["updated_at"], reverse=True),
        "activity": activity, "updated_at": pr["updatedAt"],
    }


REVIEW_TEXT = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes requested", "COMMENTED": "commented", "DISMISSED": "dismissed"}
REASON_TEXT = {"review_requested": "your review was requested", "mention": "you were mentioned", "team_mention": "your team was mentioned",
               "author": "you are the author", "comment": "you commented", "state_change": "you changed its state",
               "ci_activity": "CI activity", "assign": "it was assigned to you", "subscribed": "you watch the repo",
               "manual": "you subscribed"}


def _explain(action, v):
    """Human explanation of why a PR got its action (v = classify() locals)."""
    pr, state = v["pr"], v["state"]
    if action == "review":
        if v["requested_me"] and not v["requested_teams"]:
            return "Your review was requested directly."
        teams = ", ".join("@" + t for t in v["requested_teams"])
        return (f"A review was requested from your team ({teams}) and you haven't done it"
                + (" on the latest commit." if v["my_review"] else "."))
    if action == "changes":
        return "Changes were requested: " + ", ".join(r["login"] for r in v["blocking"] if not r["stale"]) + "."
    if action == "conflicts":
        return "Your PR has conflicts with the base branch: they must be resolved before merging."
    if action == "threads":
        n = len(v["threads_waiting"])
        return (f"{n} unresolved review conversation{'s' if n > 1 else ''} waiting for your reply "
                f"({', '.join(sorted({t['last_by'] for t in v['threads_waiting']}))}).")
    if action == "update":
        return "Approved with a green pipeline, but the branch is behind the base and the repo requires updating it."
    if action == "ci":
        names = ", ".join(c["name"] for c in v["failing"][:3]) or "see checks"
        return f"Your PR's pipeline is failing ({names})."
    if action == "mention":
        m = [m for m in v["mentions"] if not m["answered"]][-1]
        return f"{m['by']} mentioned you and you haven't replied since."
    if action == "reapproval":
        return "All approvals are on commits older than the latest one: ask for them again."
    if action == "merge":
        return "Approved, pipeline green and ready to merge."
    if action == "waiting":
        if pr["isDraft"]:
            return "Your PR is a draft."
        if v["ci_state"] in ("PENDING", "EXPECTED"):
            return "The pipeline is running."
        return "Waiting for reviews."
    # fyi: no action for you right now; say why
    if state in ("MERGED", "CLOSED"):
        return f"The PR is {'merged' if state == 'MERGED' else 'closed'}; nothing to do."
    if v["my_review"]:
        mr = v["my_review"]
        txt = f"You already reviewed it ({REVIEW_TEXT.get(mr['state'], mr['state'].lower())})"
        if v["stale"](mr):
            return txt + ". There are new commits since your review, but your review wasn't requested again."
        return txt + " and there are no new commits since."
    if v["requested_teams"]:
        return "The review was requested from a team, not you, and you got no review request."
    reasons = [REASON_TEXT.get(r, r) for r in v["reasons"]]
    return ("You are not a reviewer. It shows up because " + " and ".join(reasons) + "." if reasons
            else "You are not a reviewer and weren't mentioned; it shows up because it is your PR or has a review request.")


def _role(v):
    me_l, role = v["me_l"], []
    if v["mine"]:
        role.append("author")
    if v["my_review"]:
        role.append("reviewer (" + REVIEW_TEXT.get(v["my_review"]["state"], v["my_review"]["state"].lower())
                    + (", outdated" if v["stale"](v["my_review"]) else "") + ")")
    elif any(r["login"].lower() == me_l and r["state"] == "PENDING" for r in v["reviewers"].values()):
        role.append("pending reviewer")
    if v["requested_teams"]:
        role.append("reviewing team: " + ", ".join("@" + t for t in v["requested_teams"]))
    if v["mentions"]:
        role.append("mentioned")
    if "comment" in v["reasons"]:
        role.append("you commented")
    return role


def _excerpt(body, rx, width=160):
    text = re.sub(r"\s+", " ", body).strip()
    m = rx and rx.search(text)
    start = max(0, m.start() - width // 2) if m else 0
    return ("…" if start else "") + text[start:start + width] + ("…" if start + width < len(text) else "")


# --- app -----------------------------------------------------------------


def _since():
    return (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Account:
    """One GitHub identity: its client, its sync state and its slice of the store (keys prefixed "<id>|")."""

    def __init__(self, store, row):
        self.store = store
        self.cfg = dict(row)
        self.id = row["id"]
        self.login = row["login"]
        self._gh = None
        self.error = None  # shown in the UI
        self.last_failure = None  # latest failure, even while still within TRANSIENT_GRACE
        self.failures = 0
        self.last_attempt = None
        self.last_full = None  # time.time() of the last full re-read; None forces one
        self.cost = {}  # GraphQL points of the last "light" and "full" sync
        self.syncing = False

    @property
    def gh(self):
        if self._gh is None:
            provider = self._gh_token if self.cfg["source"] == "gh" else (lambda: acc.keyring_load(self.id))
            self._gh = GitHub(self.cfg["host"], provider)
        return self._gh

    def _gh_token(self):
        """`gh` files tokens under the login it saw at `gh auth login`, which a GitHub rename doesn't update.
        `gh_user` keeps that name when it differs from the real login."""
        host, name = self.cfg["host"], self.meta("gh_user") or self.login
        try:
            return acc.gh_token(host, name)
        except RuntimeError:
            if name == self.login:
                raise
        token = acc.gh_token(host, self.login)  # `gh` was logged in again, under the new name
        self.set_meta("gh_user", None)
        return token

    def refresh_login(self):
        """Follow a GitHub rename. A stale login is silent: searching for it finds nothing, without an error,
        and classify() stops recognising the user. The account id stays, since every stored key uses it."""
        login = self.gh.viewer()
        if login == self.login:
            return
        if not LOGIN_RE.fullmatch(login):
            raise ValueError(f"Unexpected login: {login}")
        old = self.login
        print(f"{now_iso()} {self.id} is now {login} (was {old})", flush=True)
        if self.cfg["source"] == "gh" and not self.meta("gh_user"):
            self.set_meta("gh_user", old)
        self.store.x("UPDATE accounts SET login=? WHERE id=?", login, self.id)
        self.store.x("UPDATE accounts SET label=? WHERE id=? AND label=?", login, self.id, old)
        if self.cfg["label"] == old:
            self.cfg["label"] = login
        self.cfg["login"] = self.login = login
        self.set_meta("teams_at", None)  # teams are looked up by login

    def meta(self, k, default=None):
        return self.store.get(f"{k}:{self.id}", default)

    def set_meta(self, k, v):
        self.store.set(f"{k}:{self.id}", v)

    def key(self, repo, number):
        return f"{self.id}|{repo}#{number}"

    def sync(self):
        self.syncing = True
        self.last_attempt = now_iso()
        spent = self._gh.spent if self._gh else 0
        try:
            kind = self._sync()
            self.error = self.last_failure = None
            self.failures = 0
            self.cost[kind] = self.gh.spent - spent
        except Exception as e:  # keep the loop alive; surface the error in the UI
            self.failures += 1
            self.last_failure = str(e)
            transient = getattr(e, "transient", False)
            print(f"{now_iso()} sync of {self.id} failed ({self.failures} in a row): {e}", flush=True)
            if not transient and not isinstance(e, RuntimeError):
                traceback.print_exc()  # unexpected: a bug, keep the trace
            # A GitHub hiccup or a network blip only shows once it persists; the data on screen stays meanwhile.
            if not transient or self.failures >= TRANSIENT_GRACE:
                self.error = str(e)
        finally:
            self.syncing = False

    def _sync(self):
        st, gh = self.store, self.gh
        full = self.last_full is None or time.time() - self.last_full >= FULL_REFRESH
        if full:
            self.refresh_login()  # before the searches, which use it
        threads, lm, _ = gh.notifications(_since(), self.meta("last_modified"))
        changed = set()
        if threads is not None:
            known = {r["id"]: r["updated_at"] for r in st.q("SELECT id, updated_at FROM threads WHERE account=?", self.id)}
            for t in threads:
                subj = t["subject"]
                if subj["type"] != "PullRequest" or not subj.get("url"):
                    continue
                key = self.key(t["repository"]["full_name"], subj["url"].rsplit("/", 1)[1])
                if known.get(t["id"]) != t["updated_at"]:
                    changed.add(key)
                st.x("INSERT OR REPLACE INTO threads(id, pr_key, reason, unread, updated_at, repo, title, account) "
                     "VALUES(?,?,?,?,?,?,?,?)", t["id"], key, t["reason"], int(t["unread"]), t["updated_at"],
                     t["repository"]["full_name"], subj["title"], self.id)
            self.set_meta("last_modified", lm)

        def keys(q):
            return {self.key(*k.rsplit("#", 1)) for k in gh.search_prs(q)}
        review = keys(f"is:pr is:open review-requested:{self.login} archived:false")
        # PRs you reviewed without being asked: GitHub sends no notification for your own review.
        reviewed = keys(f"is:pr reviewed-by:{self.login} updated:>={_since()[:10]} archived:false")
        search = review | keys(f"is:pr is:open author:{self.login} archived:false") | reviewed
        self.set_meta("search_keys", sorted(search))
        self.set_meta("review_keys", sorted(review))

        cached = {r["key"]: json.loads(r["data"]) for r in st.q(
            "SELECT key, data FROM prs WHERE key LIKE ? ESCAPE '\\'", like_prefix(self.id + "|"))}
        repo_key = lambda k: k.split("|", 1)[1]
        relevant = self.relevant_keys()
        # New PRs and PRs with new notifications are always re-read. Open (or searched) ones may have changed
        # without a notification (CI, pushes, the base branch): a cheap query says which did, and every
        # FULL_REFRESH all of them are re-read anyway, for anything the cheap query can't see.
        must = [k for k in relevant if k not in cached or k in changed]
        maybe = [k for k in relevant if k in cached and k not in changed
                 and (k in search or (cached[k] or {}).get("state") == "OPEN")]
        if maybe and not full:
            light = gh.pull_requests([repo_key(k) for k in maybe], batch=30, fragment=LIGHT_FRAGMENT)
            maybe = [k for k in maybe if fingerprint(light.get(repo_key(k))) != fingerprint(cached[k])]
        to_fetch = must + maybe
        nodes = gh.pull_requests([repo_key(k) for k in to_fetch]) if to_fetch else {}
        for k in to_fetch:
            st.x("INSERT OR REPLACE INTO prs VALUES(?,?,?)", k, json.dumps(nodes.get(repo_key(k))), now_iso())
        self.prune()
        self.set_meta("last_sync", now_iso())
        self.refresh_teams()
        if full:
            self.last_full = time.time()
        return "full" if full else "light"

    def prune(self):
        """Drop notification threads older than the window and cached PRs (and marks) no longer shown."""
        st, pre = self.store, like_prefix(self.id + "|")
        st.x("DELETE FROM threads WHERE account=? AND updated_at < ?", self.id, _since())
        keep = set(self.relevant_keys())
        stale = [r["key"] for r in st.q("SELECT key FROM prs WHERE key LIKE ? ESCAPE '\\'", pre) if r["key"] not in keep]
        stale += [r["pr_key"] for r in st.q("SELECT pr_key FROM marks WHERE pr_key LIKE ? ESCAPE '\\' AND kind != 'mute'", pre)
                  if r["pr_key"] not in keep]
        for k in stale:
            st.x("DELETE FROM prs WHERE key=?", k)
            st.x("DELETE FROM marks WHERE pr_key=? AND kind != 'mute'", k)

    def refresh_teams(self, max_age_hours=6):
        at = self.meta("teams_at")
        if at and datetime.now(timezone.utc) - parse_ts(at) < timedelta(hours=max_age_hours):
            return
        self.set_meta("teams", self.gh.my_teams(self.login))
        self.set_meta("teams_at", now_iso())

    def relevant_keys(self):
        keys = {r["pr_key"] for r in self.store.q(
            "SELECT DISTINCT pr_key FROM threads WHERE account=? AND updated_at >= ?", self.id, _since())}
        return sorted(keys | set(self.meta("search_keys", [])))

    def public(self):
        rest, graphql = endpoints(self.cfg["host"])
        return {**{k: self.cfg[k] for k in ("id", "source", "host", "login", "label", "color")},
                "enabled": bool(self.cfg["enabled"]), "error": self.error, "syncing": self.syncing,
                "failures": self.failures, "last_failure": self.last_failure, "cost": self.cost,
                "last_sync": self.meta("last_sync"), "last_attempt": self.last_attempt,
                "rate": self._gh.rate if self._gh else {}, "scopes": self._gh.scopes if self._gh else None,
                "endpoints": {"rest": rest, "graphql": graphql}, "gh_user": self.meta("gh_user") or self.login}


class Inbox:
    def __init__(self):
        self.store = Store(DATA_DIR / "state.db")
        apply_sync_settings(self.store)
        self.wake = threading.Event()
        self.lock = threading.RLock()
        if not self.store.q("SELECT 1 FROM accounts LIMIT 1"):
            self._bootstrap_account()
        self.accounts = {}
        self.reload_accounts()

    def _bootstrap_account(self):
        """First run: use the active `gh` account. Without one the app still starts and explains what to do;
        each sync tries again, so a `gh auth login` done later is picked up on its own."""
        found = acc.gh_accounts()
        if not found:
            return
        a = next((a for a in found if a["active"]), found[0])
        aid = self._insert_account("gh", a["host"], a["login"], a["login"])
        self.store.migrate_single_account(aid)

    def _check_new_account(self, host, login):
        """An account keeps its id after a rename, so the id alone doesn't catch a duplicate."""
        if self.store.q("SELECT 1 FROM accounts WHERE id=? OR (host=? AND lower(login)=?)",
                        f"{login}@{host}".lower(), host, login.lower()):
            raise ValueError(f"Account {login} ({host}) is already added")

    def _insert_account(self, source, host, login, label):
        aid = f"{login}@{host}".lower()
        self._check_new_account(host, login)
        n = len(self.store.q("SELECT id FROM accounts"))
        self.store.x("INSERT INTO accounts VALUES(?,?,?,?,?,?,1,?)", aid, source, host, login, label,
                     ACCOUNT_COLORS[n % len(ACCOUNT_COLORS)], n)
        return aid

    def reload_accounts(self):
        with self.lock:
            rows = self.store.q("SELECT * FROM accounts ORDER BY pos")
            old = self.accounts
            self.accounts = {}
            for r in rows:
                a = old.get(r["id"]) or Account(self.store, r)
                a.cfg = dict(r)
                self.accounts[r["id"]] = a

    def enabled(self):
        return [a for a in self.accounts.values() if a.cfg["enabled"]]

    def account_of(self, key):
        return self.accounts[key.split("|", 1)[0]]

    # sync ------------------------------------------------------------

    def sync(self):
        if not self.accounts:
            self._bootstrap_account()
            self.reload_accounts()
        for a in self.enabled():
            a.sync()
        self.desktop_notify()

    def force_full_sync(self):
        for a in self.accounts.values():
            a.last_full = None
        self.wake.set()

    def loop(self):
        while True:
            trim_log()
            self.sync()
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()

    # accounts ----------------------------------------------------------

    def available_gh_accounts(self):
        added = {(a.cfg["host"], name.lower()) for a in self.accounts.values()
                 for name in (a.login, a.meta("gh_user") or a.login)}
        return [a for a in acc.gh_accounts() if (a["host"], a["login"].lower()) not in added]

    def add_account(self, body):
        if body.get("source") == "gh":
            host, login = body["host"], body["login"]
            if not any(a["host"] == host and a["login"] == login for a in acc.gh_accounts()):
                raise ValueError(f"{login} ({host}) is not logged in with gh")
            real = GitHub(host, lambda: acc.gh_token(host, login)).viewer()  # fails early if gh can't provide a token
            if not LOGIN_RE.fullmatch(real):
                raise ValueError(f"Unexpected login: {real}")
            aid = self._insert_account("gh", host, real, body.get("label") or real)
            if real != login:  # renamed since `gh auth login`
                self.store.set(f"gh_user:{aid}", login)
        elif body.get("source") == "token":
            host = (body.get("host") or "github.com").strip().lower().removeprefix("https://").strip("/") or "github.com"
            if not HOST_RE.fullmatch(host):
                raise ValueError(f"Invalid server: {host}")
            token = (body.get("token") or "").strip()
            if not token:
                raise ValueError("The token is missing")
            gh = GitHub(host, lambda: token)
            login = gh.viewer()
            if not LOGIN_RE.fullmatch(login):
                raise ValueError(f"Unexpected login: {login}")
            gh.rate_limit()
            missing = [sc for sc in ("repo", "read:org") if gh.scopes is not None and sc not in gh.scopes]
            if gh.scopes is not None and "notifications" not in gh.scopes and "repo" not in gh.scopes:
                missing.append("notifications")
            if missing:
                raise ValueError("The token is missing scopes: " + ", ".join(missing))
            self._check_new_account(host, login)
            aid = f"{login}@{host}".lower()
            acc.keyring_store(aid, token)
            self._insert_account("token", host, login, body.get("label") or login)
        else:
            raise ValueError("Unknown account source")
        self.reload_accounts()
        self.wake.set()
        return aid

    def update_account(self, body):
        a = self.accounts[body["id"]]
        if body.get("color") and not COLOR_RE.fullmatch(str(body["color"])):
            raise ValueError("Invalid color")
        for field in ("label", "color"):
            if body.get(field):
                self.store.x(f"UPDATE accounts SET {field}=? WHERE id=?", str(body[field])[:60], a.id)
        if "enabled" in body:
            self.store.x("UPDATE accounts SET enabled=? WHERE id=?", int(bool(body["enabled"])), a.id)
        self.reload_accounts()
        self.wake.set()

    def remove_account(self, aid):
        a = self.accounts[aid]
        if len(self.accounts) == 1:
            raise ValueError("You cannot remove the only account")
        pre = like_prefix(aid + "|")
        with self.store.lock:
            for sql in ("DELETE FROM threads WHERE account=?", "DELETE FROM accounts WHERE id=?"):
                self.store.db.execute(sql, (aid,))
            for sql in ("DELETE FROM prs WHERE key LIKE ? ESCAPE '\\'", "DELETE FROM marks WHERE pr_key LIKE ? ESCAPE '\\'"):
                self.store.db.execute(sql, (pre,))
            self.store.db.execute("DELETE FROM meta WHERE k IN (%s)" % ",".join("?" * len(ACCOUNT_META)),
                                  [f"{k}:{aid}" for k in ACCOUNT_META])
            self.store.db.commit()
        if a.cfg["source"] == "token":
            acc.keyring_delete(aid)
        self.reload_accounts()

    def refresh_rates(self):
        for a in self.enabled():
            try:
                a.gh.rate_limit()
            except Exception as e:
                a.error = str(e)

    # view ------------------------------------------------------------

    def items(self):
        st = self.store
        threads = {}
        for r in st.q("SELECT * FROM threads"):
            threads.setdefault(r["pr_key"], []).append(
                {"id": r["id"], "reason": r["reason"], "unread": bool(r["unread"]), "updated_at": r["updated_at"]})
        prs = {r["key"]: json.loads(r["data"]) for r in st.q("SELECT key, data FROM prs")}
        marks = {r["pr_key"]: dict(r) for r in st.q("SELECT * FROM marks")}
        now = now_iso()
        out = []
        for a in self.enabled():
            review_keys = set(a.meta("review_keys", []))
            for key in a.relevant_keys():
                if not prs.get(key):
                    continue
                item = classify(key, prs[key], threads.get(key, []), a.login, key in review_keys)
                item["account"] = a.id
                mark = marks.get(key)
                item["tab"] = "inbox"
                if mark:
                    urgent = ACTIONS[item["action"]][1]
                    if mark["kind"] == "mute":
                        item["tab"] = "muted"
                    elif mark["kind"] == "snooze" and mark["until"] > now:
                        item["tab"] = "snoozed"
                    elif mark["kind"] == "done" and item["activity"] <= mark["at"] and not (
                            urgent and item["action"] != mark["action"]):
                        item["tab"] = "done"
                    else:  # snooze expired or new activity after "done": back to the inbox
                        st.x("DELETE FROM marks WHERE pr_key=?", key)
                        mark = None
                item["mark"] = mark
                out.append(item)
        return out

    def state(self):
        accounts = [a.public() for a in self.accounts.values()]
        enabled = self.enabled()
        teams = [{**t, "account": a.id} for a in enabled for t in a.meta("teams", [])]
        return {"me": enabled[0].login if enabled else None, "mes": sorted({a.login for a in enabled}),
                "accounts": accounts, "teams": teams, "syncing": any(a.syncing for a in enabled),
                "days": DAYS, "poll": POLL_SECONDS, "limits": SYNC_LIMITS, "full_refresh": FULL_REFRESH,
                "version": VERSION, "app_dir": str(HERE),
                "notify": self.store.get("notify", True), "notify_actions": self.notify_actions(),
                "notify_labels": NOTIFY_LABELS, "quiet": self.quiet(), "quiet_now": self.quiet_now(),
                "items": self.items()}

    # marks -----------------------------------------------------------

    def mark(self, key, kind, until=None):
        item = next((i for i in self.items() if i["key"] == key), None)
        if not item:
            raise KeyError(key)
        if kind == "snooze":
            try:
                until = parse_ts(until).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (AttributeError, TypeError, ValueError):
                raise ValueError("Invalid snooze date") from None
        else:
            until = None
        gh = self.account_of(key).gh
        self.store.x("INSERT OR REPLACE INTO marks VALUES(?,?,?,?,?)", key, kind, now_iso(), until, item["action"])
        # Keep GitHub in sync: done/mute mark the threads read, mute also unsubscribes.
        for t in item["threads"]:
            if kind in ("done", "mute") and t["unread"]:
                gh.mark_thread_read(t["id"])
                self.store.x("UPDATE threads SET unread=0 WHERE account=? AND id=?", item["account"], t["id"])
            if kind == "mute":
                gh.set_thread_ignored(t["id"], True)

    def unmark(self, key):
        rows = self.store.q("SELECT kind FROM marks WHERE pr_key=?", key)
        if rows and rows[0]["kind"] == "mute":
            gh = self.account_of(key).gh
            for r in self.store.q("SELECT id FROM threads WHERE pr_key=?", key):
                gh.set_thread_ignored(r["id"], False)
        self.store.x("DELETE FROM marks WHERE pr_key=?", key)

    # desktop notifications ---------------------------------------------

    def notify_actions(self):
        return self.store.get("notify_actions", ["comment"] + [a for a, (_, urgent) in ACTIONS.items() if urgent])

    def quiet(self):
        return {**DEFAULT_QUIET, **self.store.get("quiet", {})}

    def quiet_now(self, now=None):
        q = self.quiet()
        if not q["enabled"]:
            return False
        now = now or datetime.now()
        if q["weekends"] and now.weekday() >= 5:
            return True
        t = now.strftime("%H:%M")
        start, end = q["from"], q["to"]
        return (start <= t < end) if start <= end else (t >= start or t < end)

    def set_settings(self, body):
        if "notify" in body:
            self.store.set("notify", bool(body["notify"]))
        if "notify_actions" in body:
            self.store.set("notify_actions", [a for a in body["notify_actions"] if a in NOTIFY_LABELS])
        if "quiet" in body:
            q = {**self.quiet(), **{k: v for k, v in body["quiet"].items() if k in DEFAULT_QUIET}}
            for k in ("from", "to"):
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(q[k])):
                    raise ValueError(f"Invalid time: {q[k]}")
            q["enabled"], q["weekends"] = bool(q["enabled"]), bool(q["weekends"])
            self.store.set("quiet", q)
        old_days = DAYS
        for k, (lo, hi) in SYNC_LIMITS.items():
            if k in body:
                try:
                    v = int(body[k])
                except (TypeError, ValueError):
                    raise ValueError(f"Invalid {k}: {body[k]}") from None
                if not lo <= v <= hi:
                    raise ValueError(f"{'Days' if k == 'days' else 'Sync interval'} must be between {lo} and {hi}")
                self.store.set(k, v)
        if "days" in body or "poll" in body:
            apply_sync_settings(self.store)
            if DAYS > old_days:  # older notifications aren't in the cached ones: read them all again
                for a in self.accounts.values():
                    a.set_meta("last_modified", None)
            self.force_full_sync()

    def visible(self):
        """Predicate: does an item pass the filter dropdowns saved by the UI (not the search box)?"""
        f = self.store.get("ui_prefs", {}).get("f") or {}
        fields = {"accounts": lambda i: i["account"], "repos": lambda i: i["repo"],
                  "authors": lambda i: i["author"]["login"], "actions": lambda i: i["action"], "states": lambda i: i["state"]}
        return lambda i: all(not f.get(k) or get(i) in f[k] for k, get in fields.items())

    def badge(self):
        """PRs in the Inbox that pass the UI filters and need something from you."""
        visible = self.visible()
        return sum(1 for i in self.items() if i["tab"] == "inbox" and i["action"] in BADGE_ACTIONS and visible(i))

    def desktop_notify(self):
        """Notify when a PR's action changes (pushes alone don't notify) or someone else comments on it.
        Snoozed PRs still notify comments; done PRs come back to the Inbox on new activity anyway."""
        log = self.store.get("notified_state")
        legacy = self.store.get("notified_actions")  # previous format: actions only
        first_run = log is None and legacy is None
        if log is None and legacy is not None:
            # Comments become a notification kind: turn them on, and take the existing ones as seen.
            log = {"at": now_iso(), "prs": {k: {"action": a, "comment": "~"} for k, a in legacy.items()}}
            if self.store.get("notify_actions") is not None:
                self.store.set("notify_actions", ["comment"] + [a for a in self.notify_actions() if a != "comment"])
        log = log or {"at": now_iso(), "prs": {}}
        wanted = set(self.notify_actions())
        visible = self.visible()  # PRs hidden by the filters don't notify (but are still logged: no burst on unfiltering)
        fresh, current = [], {}
        for i in self.items():
            prev = log["prs"].get(i["key"])
            comment_at = (i["last_comment"] or {}).get("at")
            if i["tab"] in ("done", "muted"):
                if prev:
                    current[i["key"]] = prev
                continue
            # A snoozed PR keeps its last notified action (a change notifies when it comes back); comments don't wait.
            action = prev["action"] if prev and i["tab"] == "snoozed" else i["action"]
            seen_comment = (prev["comment"] if prev else log["at"]) or ""  # new PR: comments since the last run
            if seen_comment == "~":
                seen_comment = comment_at or ""
            current[i["key"]] = {"action": action, "comment": max(comment_at or "", seen_comment) or None}
            if not visible(i):
                continue
            if i["tab"] == "inbox" and action in wanted and (not prev or prev["action"] != action):
                fresh.append((i, NOTIFY_LABELS[action]))
            elif comment_at and comment_at > seen_comment and "comment" in wanted:
                fresh.append((i, f"{NOTIFY_LABELS['comment']} from {i['last_comment']['by']}"))
        # Only PRs still in the window are kept, so the log can't grow forever.
        self.store.set("notified_state", {"at": now_iso(), "prs": current})
        self.store.x("DELETE FROM meta WHERE k IN ('notified', 'notified_actions')")  # older formats
        # Quiet hours swallow notifications (they are still recorded: no burst when the quiet period ends).
        if (first_run or not fresh or not self.store.get("notify", True) or self.quiet_now()
                or not shutil.which("notify-send")):
            return
        multi = len(self.enabled()) > 1
        icon = str(STATIC / "icon.svg")
        for i, label in fresh[:5]:
            who = f"[{self.accounts[i['account']].cfg['label']}] " if multi else ""
            body = i["title"]
            if label.startswith(NOTIFY_LABELS["comment"]):
                body += "\n" + i["last_comment"]["excerpt"]
            subprocess.Popen(["notify-send", "-a", "PR Inbox", "-i", icon, "--",
                              f"{who}{label} · {i['repo'].split('/')[-1]}#{i['number']}",
                              html.escape(body)])  # GNOME renders markup in the body
        if len(fresh) > 5:
            subprocess.Popen(["notify-send", "-a", "PR Inbox", "-i", icon, "--", f"+{len(fresh) - 5} more PR notifications"])


# --- HTTP ----------------------------------------------------------------


# The page is self-contained: no third-party scripts, styles or connections; images are GitHub avatars.
# Scripts need a per-response nonce, so markup injected by a PR title or comment could never run code.
CSP = ("default-src 'none'; script-src {script}; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' https: data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


LOCKED_PAGE = b"""<!doctype html><meta charset="utf-8"><title>PR Inbox</title>
<p style="font:15px system-ui;margin:3em;text-align:center">Open PR Inbox from its launcher.</p>"""


def make_handler(inbox, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", headers=None, nonce=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", CSP.format(script=f"'nonce-{nonce}'" if nonce else "'none'"))
            self.end_headers()
            self.wfile.write(data)

        def _trusted(self):
            """Only our own page may talk to the API. Checking Host defeats DNS rebinding (evil.com resolving to
            127.0.0.1 would otherwise be same-origin); checking Origin blocks cross-site requests."""
            port = self.server.server_address[1]
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origin = self.headers.get("Origin")
            if self.headers.get("Host") not in allowed or (origin is not None and origin not in {f"http://{h}" for h in allowed}):
                self._send(403, {"error": "forbidden"})
                return False
            # The secret proves the caller can read our private data directory (i.e. it is this user).
            path = self.path.split("?")[0]
            if path == "/api/ping":
                return True
            cookie = SimpleCookie(self.headers.get("Cookie") or "").get(auth_cookie(port))
            given = self.headers.get("X-PR-Inbox-Token") or (cookie.value if cookie else "")
            if not hmac.compare_digest(given.encode(), token.encode()):
                if path == "/" and self.command == "GET":
                    self._send(401, LOCKED_PAGE, "text/html; charset=utf-8")
                else:
                    self._send(401, {"error": "unauthorized"})
                return False
            return True

        def _login(self):
            """The window opens /auth?k=<secret>: swap it for an HttpOnly cookie and go to the app."""
            given = parse_qs(urlsplit(self.path).query).get("k", [""])[0]
            if not hmac.compare_digest(given.encode(), token.encode()):
                return self._send(401, LOCKED_PAGE, "text/html; charset=utf-8")
            port = self.server.server_address[1]
            # Continue from our own page: after a redirect from file:// (Chrome mode) the SameSite=Strict
            # cookie would not be sent, since that navigation counts as cross-site.
            self._send(200, b'<!doctype html><meta http-equiv="refresh" content="0;url=/">', "text/html; charset=utf-8",
                       {"Set-Cookie": f"{auth_cookie(port)}={token}; Path=/; HttpOnly; SameSite=Strict"})

        def do_GET(self):
            if self.path.split("?")[0] == "/auth":
                return self._login()
            if not self._trusted():
                return
            path = self.path.split("?")[0]
            if path == "/api/state":
                return self._send(200, inbox.state())
            if path == "/api/badge":
                return self._send(200, {"count": inbox.badge()})
            if path == "/api/ping":
                # The proof lets the window check this is its own backend (not another user's, or a squatter
                # on the port) before handing it the secret. It reveals nothing about the secret.
                n = parse_qs(urlsplit(self.path).query).get("n", [""])[0][:64]
                proof = hmac.new(token.encode(), n.encode(), "sha256").hexdigest() if n else None
                return self._send(200, {"ok": True, "app": "pr-inbox", "version": VERSION, "proof": proof})
            if path == "/api/prefs":
                return self._send(200, inbox.store.get("ui_prefs", {}))
            if path == "/api/accounts/available":
                return self._send(200, {"gh": inbox.available_gh_accounts()})
            name = "index.html" if path == "/" else path.removeprefix("/static/")
            f = (STATIC / name).resolve()
            if f.is_file() and STATIC in f.parents:
                ctype = {".html": "text/html; charset=utf-8", ".svg": "image/svg+xml"}.get(f.suffix, "application/octet-stream")
                data, nonce = f.read_bytes(), None
                if f.suffix == ".html":
                    nonce = secrets.token_urlsafe(16)
                    data = data.replace(b"<script>", f'<script nonce="{nonce}">'.encode())
                return self._send(200, data, ctype, nonce=nonce)
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._trusted():
                return
            # JSON only: a cross-site form can't send it without a CORS preflight, which we never grant.
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._send(415, {"error": "json only"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "bad length"})
            if not 0 <= length <= MAX_BODY:
                return self._send(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("expected an object")
            except ValueError as e:
                return self._send(400, {"error": f"bad json: {e}"})
            try:
                if self.path == "/api/prefs":  # UI filters/tab/collapsed repos, persisted in SQLite
                    inbox.store.set("ui_prefs", body)
                    return self._send(200, {"ok": True})
                if self.path == "/api/sync":  # the user asked: re-read everything, not just what changed
                    inbox.force_full_sync()
                elif self.path == "/api/mark":
                    if body["kind"] not in ("done", "snooze", "mute"):
                        return self._send(400, {"error": "bad kind"})
                    inbox.mark(body["key"], body["kind"], body.get("until"))
                elif self.path == "/api/unmark":
                    inbox.unmark(body["key"])
                elif self.path == "/api/settings":
                    inbox.set_settings(body)
                elif self.path == "/api/accounts/add":
                    inbox.add_account(body)
                elif self.path == "/api/accounts/update":
                    inbox.update_account(body)
                elif self.path == "/api/accounts/remove":
                    inbox.remove_account(body["id"])
                elif self.path == "/api/accounts/rates":
                    inbox.refresh_rates()
                else:
                    return self._send(404, {"error": "not found"})
            except (ValueError, KeyError) as e:
                return self._send(400, {"error": str(e).strip("'")})
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"error": str(e)})
            self._send(200, inbox.state())

    return Handler


def serve(port=PORT):
    """Start sync loop + HTTP server in background threads. Returns the server."""
    inbox = Inbox()
    threading.Thread(target=inbox.loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(inbox, secret_token()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="sync once and exit")
    ap.add_argument("--dump", action="store_true", help="with --once: print items as JSON")
    args = ap.parse_args()
    if args.once:
        inbox = Inbox()
        inbox.sync()
        if not inbox.accounts:
            sys.exit("No GitHub account: run `gh auth login` (or add a personal token in the app's Settings).")
        errors = [f"{a.id}: {a.last_failure}" for a in inbox.enabled() if a.last_failure]
        if errors:
            sys.exit("sync failed: " + "; ".join(errors))
        if args.dump:
            json.dump(inbox.state(), sys.stdout, indent=2, ensure_ascii=False)
        return
    serve()
    print(f"PR Inbox on http://127.0.0.1:{PORT}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
