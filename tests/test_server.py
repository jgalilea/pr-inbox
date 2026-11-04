"""Backend tests (stdlib only): python3 -m unittest discover -s tests"""
import hmac
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

ME = "me"
HEAD = "abc123"


def who(login, typename="User"):
    return {"__typename": typename, "login": login, "avatarUrl": f"https://avatars.githubusercontent.com/{login}"}


def pr_node(author=ME, state="OPEN", draft=False, decision=None, reviews=(), requests=(), comments=(), threads=(),
            checks=(), rollup=None, mergeable="MERGEABLE", merge_state="CLEAN", **extra):
    """Minimal GraphQL PullRequest node as returned with github.PR_FRAGMENT."""
    node = {
        "number": 7, "title": "Add <b>thing</b>", "url": "https://github.com/o/r/pull/7", "state": state,
        "isDraft": draft, "merged": state == "MERGED", "updatedAt": "2026-10-20T10:00:00Z",
        "createdAt": "2026-10-19T10:00:00Z", "body": "", "headRefOid": HEAD, "author": who(author),
        "repository": {"nameWithOwner": "o/r"}, "reviewDecision": decision,
        "mergeable": mergeable, "mergeStateStatus": merge_state, "additions": 10, "deletions": 2, "changedFiles": 1,
        "latestReviews": {"nodes": list(reviews)},
        "reviewRequests": {"nodes": [{"requestedReviewer": r} for r in requests]},
        "reviews": {"nodes": []}, "comments": {"nodes": list(comments)},
        "reviewThreads": {"nodes": list(threads)},
        "timelineItems": {"nodes": []},
        "commits": {"nodes": [{"commit": {"committedDate": "2026-10-20T09:00:00Z", "statusCheckRollup": (
            {"state": rollup, "contexts": {"nodes": list(checks)}} if rollup else None)}}]},
    }
    node.update(extra)
    return node


def review(login, state, oid=HEAD, at="2026-10-20T11:00:00Z"):
    return {"author": who(login), "state": state, "submittedAt": at, "url": "https://github.com/o/r/pull/7#r",
            "commit": {"oid": oid}}


def comment(login, body, at="2026-10-20T12:00:00Z", url="https://github.com/o/r/pull/7#c"):
    return {"author": who(login), "body": body, "createdAt": at, "url": url}


def classify(node, threads=(), **kw):
    return server.classify(f"{ME}@github.com|o/r#7", node, list(threads), ME, **kw)


class ClassifyTest(unittest.TestCase):
    def test_direct_review_request(self):
        i = classify(pr_node(author="bob", requests=[who(ME)]))
        self.assertEqual(i["action"], "review")

    def test_my_fresh_review_means_nothing_to_do(self):
        i = classify(pr_node(author="bob", reviews=[review(ME, "APPROVED")]))
        self.assertEqual(i["action"], "fyi")

    def test_changes_requested_on_my_pr(self):
        i = classify(pr_node(decision="CHANGES_REQUESTED", reviews=[review("bob", "CHANGES_REQUESTED")]))
        self.assertEqual(i["action"], "changes")

    def test_changes_requested_before_last_push_are_not_pending(self):
        i = classify(pr_node(decision="CHANGES_REQUESTED", reviews=[review("bob", "CHANGES_REQUESTED", oid="old")]))
        self.assertNotEqual(i["action"], "changes")

    def test_conflicts_beat_ci(self):
        i = classify(pr_node(mergeable="CONFLICTING", rollup="FAILURE"))
        self.assertEqual(i["action"], "conflicts")
        self.assertTrue(i["conflicts"])

    def test_failing_ci_lists_checks_and_dedups_reruns(self):
        run = lambda conclusion, status="COMPLETED": {
            "__typename": "CheckRun", "name": "tests", "conclusion": conclusion, "status": status,
            "detailsUrl": "https://ci/1", "checkSuite": {"workflowRun": {"workflow": {"name": "CI"}}}}
        i = classify(pr_node(rollup="FAILURE", checks=[run("SUCCESS"), run("FAILURE"),
                                                      {"__typename": "StatusContext", "context": "jenkins",
                                                       "state": "PENDING", "targetUrl": "https://j/1"}]))
        self.assertEqual(i["action"], "ci")
        self.assertEqual([c["name"] for c in i["ci"]["failing"]], ["tests"])
        self.assertEqual([c["name"] for c in i["ci"]["running"]], ["jenkins"])
        self.assertEqual(i["ci"]["total"], 2)

    def ci_run(self, conclusion, number, started):
        return {"__typename": "CheckRun", "name": "Jira-PR-Title", "conclusion": conclusion, "status": "COMPLETED",
                "detailsUrl": f"https://ci/{number}", "startedAt": started,
                "checkSuite": {"workflowRun": {"runNumber": number, "workflow": {"name": "Check PR Title"}}}}

    def test_superseded_failed_rerun_is_ignored(self):
        # Title edits re-run the workflow; run 16 started last but run 17 is the newest (as on GitHub's PR page).
        runs = [self.ci_run("FAILURE", 15, "2026-11-03T12:04:45Z"), self.ci_run("SUCCESS", 17, "2026-11-03T12:05:11Z"),
                self.ci_run("FAILURE", 16, "2026-11-03T12:05:21Z")]
        i = classify(pr_node(rollup="FAILURE", checks=runs))
        self.assertNotEqual(i["action"], "ci")
        self.assertEqual(i["ci"]["state"], "SUCCESS")
        self.assertEqual((i["ci"]["passed"], i["ci"]["failing"]), (1, []))
        self.assertEqual(i["key"], f"{ME}@github.com|o/r#7")

    def test_newest_run_failing_is_ci(self):
        runs = [self.ci_run("SUCCESS", 3, "2026-11-03T12:00:00Z"), self.ci_run("FAILURE", 4, "2026-11-03T11:00:00Z")]
        i = classify(pr_node(rollup="SUCCESS", checks=runs))
        self.assertEqual(i["action"], "ci")
        self.assertEqual(i["ci"]["state"], "FAILURE")

    def test_truncated_checks_fall_back_to_rollup(self):
        node = pr_node(rollup="FAILURE", checks=[self.ci_run("SUCCESS", 1, "2026-11-03T12:00:00Z")])
        node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["totalCount"] = 150
        self.assertEqual(classify(node)["action"], "ci")

    def test_mention_after_my_last_activity(self):
        i = classify(pr_node(author="bob", comments=[comment(ME, "hi", at="2026-10-20T10:00:00Z"),
                                                     comment("bob", "@me look", at="2026-10-20T12:00:00Z")]))
        self.assertEqual(i["action"], "mention")
        answered = classify(pr_node(author="bob", comments=[comment("bob", "@me look", at="2026-10-20T10:00:00Z"),
                                                            comment(ME, "done", at="2026-10-20T12:00:00Z")]))
        self.assertEqual(answered["action"], "fyi")

    def test_email_like_text_is_not_a_mention(self):
        i = classify(pr_node(author="bob", comments=[comment("bob", "mail foo@me.com")]))
        self.assertEqual(i["pending_mentions"], 0)

    def test_unresolved_thread_waiting_for_me(self):
        t = {"isResolved": False, "isOutdated": False, "path": "a.py",
             "comments": {"nodes": [comment("bob", "why?")]}}
        i = classify(pr_node(threads=[t]))
        self.assertEqual(i["action"], "threads")
        self.assertEqual(i["threads_waiting"], 1)

    def test_commenters_outdated(self):
        old = comment("bob", "nit", at="2026-10-20T08:00:00Z")  # before the last commit (09:00)
        fresh = comment("ann", "lgtm", at="2026-10-20T10:00:00Z")
        stale_thread = {"isResolved": True, "isOutdated": True, "path": "a.py",
                        "comments": {"nodes": [comment("cy", "rename", at="2026-10-20T11:00:00Z")]}}
        people = {c["login"]: c["outdated"] for c in classify(pr_node(comments=[old, fresh], threads=[stale_thread]))["commenters"]}
        self.assertEqual(people, {"bob": True, "ann": False, "cy": True})
        both = classify(pr_node(comments=[old, comment("bob", "again", at="2026-10-20T10:00:00Z")]))["commenters"]
        self.assertFalse(both[0]["outdated"])

    def test_old_cached_node_without_new_fields(self):
        """Closed PRs keep an old cached node: missing fields must not crash or invent open threads."""
        node = pr_node(state="MERGED", threads=[{"comments": {"nodes": [comment("bob", "x")]}}])
        for k in ("mergeable", "mergeStateStatus", "additions", "deletions", "changedFiles", "timelineItems"):
            node.pop(k)
        i = classify(node)
        self.assertEqual(i["action"], "fyi")
        self.assertEqual(i["open_threads"], [])

    def test_stale_approval_needs_reapproval(self):
        i = classify(pr_node(decision="REVIEW_REQUIRED", reviews=[review("bob", "APPROVED", oid="old")]))
        self.assertEqual(i["action"], "reapproval")

    def test_approved_green_is_merge_or_update_when_behind(self):
        args = dict(decision="APPROVED", reviews=[review("bob", "APPROVED")], rollup="SUCCESS")
        self.assertEqual(classify(pr_node(**args))["action"], "merge")
        self.assertEqual(classify(pr_node(merge_state="BEHIND", **args))["action"], "update")

    def test_draft_waits(self):
        self.assertEqual(classify(pr_node(draft=True))["action"], "waiting")

    def test_unsafe_urls_are_dropped(self):
        check = {"__typename": "StatusContext", "context": "evil", "state": "FAILURE", "targetUrl": "javascript:alert(1)"}
        i = classify(pr_node(rollup="FAILURE", checks=[check], url="file:///etc/passwd"))
        self.assertIsNone(i["ci"]["failing"][0]["url"])
        self.assertIsNone(i["url"])


class HelpersTest(unittest.TestCase):
    def test_safe_url(self):
        self.assertEqual(server.safe_url("https://x/y"), "https://x/y")
        for bad in ("javascript:alert(1)", " javascript:x", "data:text/html,x", "file:///x", None, 3):
            self.assertIsNone(server.safe_url(bad))

    def test_like_prefix_escapes_wildcards(self):
        self.assertEqual(server.like_prefix("a_b%c|"), "a\\_b\\%c|%")


class InboxTestCase(unittest.TestCase):
    """An Inbox on a throwaway database with two gh accounts (no GitHub calls)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(server, "DATA_DIR", Path(self.tmp.name) / "pr-inbox")
        patcher.start()
        self.addCleanup(patcher.stop)
        store = server.Store(server.DATA_DIR / "state.db")
        for n, login in enumerate(("me", "m_e")):
            store.x("INSERT INTO accounts VALUES(?,?,?,?,?,?,1,?)", f"{login}@github.com", "gh", "github.com",
                    login, login, "#000000", n)
        store.db.close()
        saved = server.DAYS, server.POLL_SECONDS  # Settings → Advanced changes these module values
        self.addCleanup(lambda: (setattr(server, "DAYS", saved[0]), setattr(server, "POLL_SECONDS", saved[1])))
        self.inbox = server.Inbox()
        self.store = self.inbox.store

    def add_pr(self, account, number, node, reason="review_requested", updated="2099-01-01T00:00:00Z"):
        key = f"{account}|o/r#{number}"
        self.store.x("INSERT OR REPLACE INTO prs VALUES(?,?,?)", key, json.dumps({**node, "number": number}), updated)
        self.store.x("INSERT OR REPLACE INTO threads VALUES(?,?,?,?,?,?,?,?)", f"t{number}{account}", key, reason, 1,
                     updated, "o/r", "t", account)
        return key


class InboxTest(InboxTestCase):
    def test_db_is_private(self):
        db = server.DATA_DIR / "state.db"
        self.assertEqual(db.stat().st_mode & 0o777, 0o600)
        self.assertEqual(db.parent.stat().st_mode & 0o777, 0o700)

    def test_remove_account_does_not_touch_lookalike_account(self):
        """Unescaped, the LIKE pattern "m_e@github.com|%" would also delete mxe@github.com's PRs."""
        self.store.x("INSERT INTO accounts VALUES('mxe@github.com','gh','github.com','mxe','mxe','#000000',1,3)")
        self.inbox.reload_accounts()
        keep = self.add_pr("mxe@github.com", 1, pr_node(author="bob"))
        self.add_pr("m_e@github.com", 2, pr_node(author="bob"))
        self.inbox.remove_account("m_e@github.com")
        self.assertEqual([r["key"] for r in self.store.q("SELECT key FROM prs")], [keep])

    def test_badge_counts_filtered_inbox_actions_only(self):
        self.add_pr("me@github.com", 1, pr_node(author="bob", requests=[who("me")]))  # review
        self.add_pr("me@github.com", 2, pr_node(draft=True), reason="author")  # waiting: not counted
        self.add_pr("m_e@github.com", 3, pr_node(author="m_e", rollup="FAILURE"), reason="author")  # ci
        self.assertEqual(self.inbox.badge(), 2)
        self.store.set("ui_prefs", {"f": {"actions": ["ci"]}})
        self.assertEqual(self.inbox.badge(), 1)

    def test_prune_drops_old_threads_and_unshown_prs(self):
        a = self.inbox.accounts["me@github.com"]
        self.add_pr("me@github.com", 1, pr_node(author="bob"))
        self.add_pr("me@github.com", 2, pr_node(author="bob"), updated="2000-01-01T00:00:00Z")
        self.store.x("INSERT INTO marks VALUES('me@github.com|o/r#2','done','x',NULL,'fyi')")
        self.store.x("INSERT INTO marks VALUES('me@github.com|o/r#9','mute','x',NULL,'fyi')")
        a.prune()
        self.assertEqual([r["key"] for r in self.store.q("SELECT key FROM prs")], ["me@github.com|o/r#1"])
        self.assertEqual([r["pr_key"] for r in self.store.q("SELECT pr_key FROM marks")], ["me@github.com|o/r#9"])

    def test_quiet_hours(self):
        self.inbox.set_settings({"quiet": {"enabled": True, "from": "19:00", "to": "09:00", "weekends": False}})
        wed = lambda h, m=0: datetime(2026, 10, 28, h, m)
        self.assertTrue(self.inbox.quiet_now(wed(22)))
        self.assertTrue(self.inbox.quiet_now(wed(8, 59)))
        self.assertFalse(self.inbox.quiet_now(wed(9)))
        self.assertFalse(self.inbox.quiet_now(datetime(2026, 10, 31, 12)))  # Saturday, weekends off
        self.inbox.set_settings({"quiet": {"weekends": True}})
        self.assertTrue(self.inbox.quiet_now(datetime(2026, 10, 31, 12)))

    def test_settings_validation(self):
        with self.assertRaises(ValueError):
            self.inbox.set_settings({"quiet": {"from": "25:00"}})
        self.inbox.set_settings({"notify_actions": ["review", "bogus"]})
        self.assertEqual(self.inbox.notify_actions(), ["review"])

    def test_snooze_needs_a_valid_date(self):
        key = self.add_pr("me@github.com", 1, pr_node(author="bob"))
        with self.assertRaises(ValueError):
            self.inbox.mark(key, "snooze", "tomorrow; DROP TABLE marks")
        with mock.patch.object(server.Account, "gh", mock.MagicMock()):
            self.inbox.mark(key, "snooze", "2099-01-01T08:00:00.000Z")
        self.assertEqual(self.store.q("SELECT until FROM marks")[0]["until"], "2099-01-01T08:00:00Z")

    def test_invalid_color_rejected(self):
        with self.assertRaises(ValueError):
            self.inbox.update_account({"id": "me@github.com", "color": "red;background:url(x)"})

    def test_gh_account_must_exist_in_gh(self):
        with mock.patch.object(server.acc, "gh_accounts", return_value=[]):
            with self.assertRaises(ValueError):
                self.inbox.add_account({"source": "gh", "host": "github.com", "login": "--help"})

    def notify(self):
        with mock.patch.object(server.subprocess, "Popen") as popen, \
                mock.patch.object(server.shutil, "which", return_value="/usr/bin/notify-send"):
            self.inbox.desktop_notify()
        return [c.args[0][-2] for c in popen.call_args_list]  # notification titles

    def started(self):
        """State as after a first (silent) run long ago."""
        self.store.set("notified_state", {"at": "2000-01-01T00:00:00Z", "prs": {}})

    def test_notifications_escape_markup(self):
        self.started()
        self.add_pr("me@github.com", 1, pr_node(author="bob", requests=[who("me")]))
        with mock.patch.object(server.subprocess, "Popen") as popen, \
                mock.patch.object(server.shutil, "which", return_value="/usr/bin/notify-send"):
            self.inbox.desktop_notify()
        args = popen.call_args_list[0].args[0]
        self.assertIn("Add &lt;b&gt;thing&lt;/b&gt;", args)
        self.assertEqual(args[-3], "--")  # a title starting with "-" is not an option

    def test_prs_hidden_by_filters_dont_notify(self):
        self.started()
        self.store.set("ui_prefs", {"f": {"repos": ["other/repo"]}})
        self.add_pr("me@github.com", 1, pr_node(author="bob", requests=[who("me")], comments=[comment("bob", "hi")]))
        self.assertEqual(self.notify(), [])
        self.store.set("ui_prefs", {"f": {}})
        self.assertEqual(self.notify(), [])  # already logged while hidden: clearing the filter doesn't fire a burst
        self.add_pr("me@github.com", 1, pr_node(author="bob", requests=[who("me")],
                                                comments=[comment("bob", "hi"), comment("bob", "again", at="2026-10-21T09:00:00Z")]))
        self.assertEqual(len(self.notify()), 1)

    def test_first_run_is_silent(self):
        self.add_pr("me@github.com", 1, pr_node(author="bob", requests=[who("me")], comments=[comment("bob", "hi")]))
        self.assertEqual(self.notify(), [])

    def test_action_changes_notify_pushes_dont(self):
        self.started()
        self.add_pr("me@github.com", 1, pr_node(author="bob", requests=[who("me")]))
        self.assertEqual(self.notify(), ["[me] Review requested · r#1"])
        # A push (new activity, same action): nothing.
        self.add_pr("me@github.com", 1, pr_node(author="bob", requests=[who("me")]), updated="2099-02-01T00:00:00Z")
        self.assertEqual(self.notify(), [])
        # I review it (fyi), then my review is requested again: notify.
        self.add_pr("me@github.com", 1, pr_node(author="bob", reviews=[review("me", "APPROVED")]))
        self.assertEqual(self.notify(), [])
        self.add_pr("me@github.com", 1, pr_node(author="bob", requests=[who("me")]))
        self.assertEqual(self.notify(), ["[me] Review requested · r#1"])

    def test_new_comments_notify_once_mine_and_bots_dont(self):
        self.started()
        c1 = comment("bob", "looks good", at="2026-10-20T12:00:00Z")
        self.add_pr("me@github.com", 1, pr_node(author="me", comments=[c1]), reason="author")
        self.assertEqual(self.notify(), ["[me] New comment from bob · r#1"])  # new PR with a comment since last run
        self.assertEqual(self.notify(), [])
        own = comment("me", "thanks", at="2026-10-20T13:00:00Z")
        bot = {**comment("sonar", "quality gate", at="2026-10-20T14:00:00Z"), "author": who("sonar", "Bot")}
        self.add_pr("me@github.com", 1, pr_node(author="me", comments=[c1, own, bot]), reason="author")
        self.assertEqual(self.notify(), [])
        c2 = comment("ann", "one more thing", at="2026-10-20T15:00:00Z")
        self.add_pr("me@github.com", 1, pr_node(author="me", comments=[c1, own, bot, c2]), reason="author")
        self.assertEqual(self.notify(), ["[me] New comment from ann · r#1"])

    def test_snoozed_pr_notifies_comments_not_action_changes(self):
        self.started()
        key = self.add_pr("me@github.com", 1, pr_node(author="bob", requests=[who("me")]))
        self.notify()
        self.store.x("INSERT INTO marks VALUES(?, 'snooze', 'x', '2099-12-31T00:00:00Z', 'review')", key)
        self.add_pr("me@github.com", 1, pr_node(author="bob", rollup="FAILURE"))  # fyi now, while snoozed
        self.assertEqual(self.notify(), [])
        self.add_pr("me@github.com", 1, pr_node(author="bob", comments=[comment("bob", "ping")]))
        self.assertEqual(self.notify(), ["[me] New comment from bob · r#1"])
        self.store.x("DELETE FROM marks")  # back in the Inbox with another action: that change notifies now
        self.add_pr("me@github.com", 1, pr_node(author="bob", comments=[comment("bob", "ping")]))
        self.assertEqual(self.notify(), [])  # fyi isn't a notification kind, but it is recorded
        self.assertEqual(self.store.get("notified_state")["prs"][key]["action"], "fyi")

    def test_migration_from_action_log_is_silent_and_enables_comments(self):
        key = self.add_pr("me@github.com", 1, pr_node(author="bob", requests=[who("me")],
                                                     comments=[comment("bob", "old")]))
        self.store.set("notified_actions", {key: "review"})
        self.store.set("notify_actions", ["review"])
        self.assertEqual(self.notify(), [])
        self.assertEqual(self.inbox.notify_actions(), ["comment", "review"])
        self.assertIsNone(self.store.get("notified_actions"))


class SyncTest(InboxTestCase):
    def test_reviewed_by_search_brings_unrequested_reviews(self):
        a = self.inbox.accounts["me@github.com"]
        gh = mock.MagicMock()
        gh.notifications.return_value = ([], None, 60)
        gh.search_prs.side_effect = lambda q: ["o/r#331"] if "reviewed-by:me " in q else []
        gh.pull_requests.return_value = {"o/r#331": pr_node(author="bob", reviews=[review("me", "APPROVED")])}
        gh.my_teams.return_value = []
        a._gh = gh
        a._sync()
        queries = [c.args[0] for c in gh.search_prs.call_args_list]
        self.assertTrue(any(q.startswith("is:pr reviewed-by:me updated:>=" + server._since()[:10]) for q in queries), queries)
        self.assertIn("me@github.com|o/r#331", a.relevant_keys())
        item = next(i for i in self.inbox.items() if i["key"] == "me@github.com|o/r#331")
        self.assertEqual(item["action"], "fyi")


    def fake_gh(self, account="me@github.com", node=None):
        a = self.inbox.accounts[account]
        gh = a._gh = mock.MagicMock()
        gh.rate = {}
        gh.notifications.return_value = (None, None, 60)  # 304: no new notifications
        gh.search_prs.side_effect = lambda q: ["o/r#5"] if "author:" in q else []
        node = node or pr_node(author="me")
        gh.pull_requests.side_effect = lambda keys, batch=10, fragment=None: {k: node for k in keys}
        gh.my_teams.return_value = []
        return a, gh

    def full_fetches(self, gh):
        return [c for c in gh.pull_requests.call_args_list if c.kwargs.get("fragment") is None]

    def test_unchanged_prs_are_not_read_again_until_the_full_refresh(self):
        a, gh = self.fake_gh()
        self.assertEqual(a._sync(), "full")
        self.assertEqual(len(self.full_fetches(gh)), 1)
        gh.pull_requests.reset_mock()
        self.assertEqual(a._sync(), "light")  # the light query says nothing changed
        self.assertEqual(self.full_fetches(gh), [])
        self.assertIs(gh.pull_requests.call_args.kwargs["fragment"], server.LIGHT_FRAGMENT)
        a.last_full -= server.FULL_REFRESH
        self.assertEqual(a._sync(), "full")
        self.assertEqual(len(self.full_fetches(gh)), 1)

    def test_changed_prs_are_read_in_full(self):
        a, gh = self.fake_gh()
        a._sync()
        newer = pr_node(author="me", updatedAt="2026-10-21T08:00:00Z")
        gh.pull_requests.side_effect = lambda keys, batch=10, fragment=None: {k: newer for k in keys}
        gh.pull_requests.reset_mock()
        a._sync()
        self.assertEqual([c.args[0] for c in self.full_fetches(gh)], [["o/r#5"]])
        self.assertEqual(json.loads(self.store.q("SELECT data FROM prs WHERE key='me@github.com|o/r#5'")[0]["data"])
                         ["updatedAt"], "2026-10-21T08:00:00Z")

    def test_light_and_full_nodes_give_the_same_fingerprint(self):
        full = pr_node(rollup="SUCCESS", checks=[{"__typename": "CheckRun", "name": "t", "status": "COMPLETED",
                                                  "conclusion": "SUCCESS", "detailsUrl": "https://ci"}])
        light = {k: full[k] for k in ("updatedAt", "headRefOid", "state", "isDraft", "mergeable", "mergeStateStatus",
                                      "reviewDecision")}
        light["commits"] = {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS", "contexts": {"nodes": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]}}}}]}
        self.assertEqual(server.fingerprint(light), server.fingerprint(full))
        light["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"][0]["status"] = "IN_PROGRESS"
        self.assertNotEqual(server.fingerprint(light), server.fingerprint(full))

    def test_temporary_github_errors_only_show_when_they_persist(self):
        from github import GitHubError
        a, gh = self.fake_gh()
        gh.notifications.side_effect = GitHubError("GitHub is temporarily unavailable (503).", transient=True)
        with mock.patch("builtins.print"):
            for n in range(1, server.TRANSIENT_GRACE):
                a.sync()
                self.assertIsNone(a.error, n)
            a.sync()
        self.assertIn("503", a.error)
        gh.notifications.side_effect = None
        a.sync()
        self.assertEqual((a.error, a.failures), (None, 0))

    def test_other_errors_show_at_once(self):
        a, gh = self.fake_gh()
        gh.notifications.side_effect = RuntimeError("gh has no session for me")
        with mock.patch("builtins.print"):
            a.sync()
        self.assertEqual(a.error, "gh has no session for me")

    def test_sync_settings(self):
        with self.assertRaises(ValueError):
            self.inbox.set_settings({"poll": 30})  # GitHub asks for 60 s at least
        with self.assertRaises(ValueError):
            self.inbox.set_settings({"days": "x"})
        a = self.inbox.accounts["me@github.com"]
        a.set_meta("last_modified", "yesterday")
        a.last_full = 1
        self.inbox.set_settings({"days": server.DAYS + 7, "poll": 300})
        self.assertEqual((self.inbox.state()["poll"], server.POLL_SECONDS), (300, 300))
        self.assertIsNone(a.meta("last_modified"))  # a longer window reads all notifications again
        self.assertIsNone(a.last_full)
        self.assertEqual(server.Inbox().state()["poll"], 300)  # kept across restarts

    def test_no_gh_account_starts_empty_and_picks_it_up_later(self):
        with mock.patch.object(server, "DATA_DIR", Path(self.tmp.name) / "fresh"), \
                mock.patch.object(server.acc, "gh_accounts", return_value=[]):
            inbox = server.Inbox()
            self.assertEqual(inbox.accounts, {})
            inbox.sync()
            found = [{"host": "github.com", "login": "new", "active": True}]
            with mock.patch.object(server.acc, "gh_accounts", return_value=found), \
                    mock.patch.object(server.Account, "sync"):
                inbox.sync()
            self.assertEqual(list(inbox.accounts), ["new@github.com"])

    def test_log_keeps_the_newest_lines(self):
        log = Path(self.tmp.name) / "pr-inbox.log"
        log.write_text("".join(f"line {n}\n" for n in range(1000)))
        self.assertFalse(server.trim_log(log, max_bytes=1 << 20))
        self.assertTrue(server.trim_log(log, max_bytes=1000, keep=100))
        lines = log.read_text().splitlines()
        self.assertIn("log trimmed", lines[0])
        self.assertEqual(lines[-1], "line 999")
        self.assertTrue(all(l.startswith("line ") for l in lines[1:]))  # no half line


class HttpTest(InboxTestCase):
    def setUp(self):
        super().setUp()
        self.token = server.secret_token()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.inbox, self.token))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Host": f"127.0.0.1:{self.port}", "X-PR-Inbox-Token": self.token, **(headers or {})}
        hdrs = {k: v for k, v in hdrs.items() if v is not None}
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        c.request(method, path, body=body, headers=hdrs)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def test_page_and_api(self):
        r, data = self.req("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))
        r, data = self.req("GET", "/api/badge")
        self.assertEqual(json.loads(data), {"count": 0})

    def test_page_scripts_need_the_response_nonce(self):
        r, data = self.req("GET", "/")
        csp = r.getheader("Content-Security-Policy")
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
        nonce = re.search(r"script-src 'nonce-([\w-]+)'", csp).group(1)
        self.assertEqual(data.count(b"<script"), data.count(f'<script nonce="{nonce}">'.encode()))
        self.assertGreater(data.count(b"<script"), 0)
        _, again = self.req("GET", "/")
        self.assertNotIn(nonce.encode(), again)  # fresh nonce per response
        r, _ = self.req("GET", "/", headers={"X-PR-Inbox-Token": None})
        self.assertIn("script-src 'none'", r.getheader("Content-Security-Policy"))

    def test_ping_proves_the_backend_knows_the_secret(self):
        r, data = self.req("GET", "/api/ping?n=abc", headers={"X-PR-Inbox-Token": None})
        self.assertEqual(json.loads(data)["proof"], hmac.new(self.token.encode(), b"abc", "sha256").hexdigest())
        _, data = self.req("GET", "/api/ping", headers={"X-PR-Inbox-Token": None})
        self.assertIsNone(json.loads(data)["proof"])

    def test_secret_is_private_and_stable(self):
        path = server.DATA_DIR / "secret"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertGreaterEqual(len(self.token), 40)
        self.assertEqual(server.secret_token(), self.token)

    def test_requests_without_the_secret_are_rejected(self):
        for headers in ({"X-PR-Inbox-Token": None}, {"X-PR-Inbox-Token": "wrong"}):
            r, _ = self.req("GET", "/api/state", headers=headers)
            self.assertEqual(r.status, 401)
            r, _ = self.req("POST", "/api/sync", {}, headers=headers)
            self.assertEqual(r.status, 401)
        r, data = self.req("GET", "/", headers={"X-PR-Inbox-Token": None})
        self.assertEqual(r.status, 401)
        self.assertIn(b"Open PR Inbox from its launcher", data)
        r, data = self.req("GET", "/api/ping", headers={"X-PR-Inbox-Token": None})  # how the window finds the backend
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(data)["version"], server.VERSION)

    def test_auth_sets_cookie_that_unlocks_the_api(self):
        r, _ = self.req("GET", "/auth?k=wrong", headers={"X-PR-Inbox-Token": None})
        self.assertEqual(r.status, 401)
        self.assertIsNone(r.getheader("Set-Cookie"))
        r, _ = self.req("GET", f"/auth?k={self.token}", headers={"X-PR-Inbox-Token": None})
        self.assertEqual(r.status, 200)  # a same-site meta refresh to "/", not a redirect (see _login)
        cookie = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        r, _ = self.req("GET", "/api/state", headers={"X-PR-Inbox-Token": None, "Cookie": cookie.split(";")[0]})
        self.assertEqual(r.status, 200)

    def test_dns_rebinding_rejected(self):
        r, _ = self.req("GET", "/api/state", headers={"Host": f"evil.example:{self.port}"})
        self.assertEqual(r.status, 403)

    def test_cross_origin_post_rejected(self):
        r, _ = self.req("POST", "/api/sync", {}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/api/sync", {}, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(r.status, 200)

    def test_post_requires_json(self):
        r, _ = self.req("POST", "/api/sync", b"x=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status, 415)
        r, _ = self.req("POST", "/api/sync", b"{nope", headers={"Content-Type": "application/json"})
        self.assertEqual(r.status, 400)
        r, _ = self.req("POST", "/api/prefs", b"[]", headers={"Content-Type": "application/json"})
        self.assertEqual(r.status, 400)

    def test_body_size_limit(self):
        r, _ = self.req("POST", "/api/prefs", b"{}", headers={"Content-Type": "application/json",
                                                              "Content-Length": str(server.MAX_BODY + 1)})
        self.assertEqual(r.status, 413)

    def test_static_path_traversal(self):
        for path in ("/static/../server.py", "/static/%2e%2e/server.py", "/../server.py"):
            r, _ = self.req("GET", path)
            self.assertEqual(r.status, 404, path)


class GitHubClientTest(unittest.TestCase):
    def http_error(self, code, body=b"", headers=None):
        import email.message
        import io
        import urllib.error
        hdrs = email.message.Message()
        for k, v in (headers or {}).items():
            hdrs[k] = v
        return urllib.error.HTTPError("https://api.github.com/x", code, "Error", hdrs, io.BytesIO(body))

    def ok_response(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = b'{"ok": true}'
        resp.status, resp.headers = 200, {}
        return resp

    def test_temporary_errors_are_retried(self):
        import github
        gh = github.GitHub("github.com", lambda: "t")
        with mock.patch.object(github, "RETRY_DELAYS", (0, 0)), mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [self.http_error(504, b"<html>Unicorn!</html>"), self.ok_response()]
            self.assertEqual(gh._request("GET", "/x")[2], {"ok": True})
            urlopen.side_effect = [self.http_error(503, b"<html>Unicorn!</html>")] * 3
            with self.assertRaises(github.GitHubError) as cm:
                gh._request("GET", "/x")
        self.assertTrue(cm.exception.transient)
        self.assertNotIn("<html>", str(cm.exception))  # no raw error pages in the UI

    def test_errors_say_what_to_do(self):
        import github
        gh = github.GitHub("github.com", lambda: "t")
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = self.http_error(401)
            with self.assertRaises(github.GitHubError) as cm:
                gh._request("GET", "/x")
            self.assertIn("gh auth login", str(cm.exception))
            urlopen.side_effect = self.http_error(403, b'{"message": "Resource not accessible"}')
            with self.assertRaises(github.GitHubError) as cm:
                gh._request("GET", "/x")
            self.assertEqual(str(cm.exception), "GitHub answered 403: Resource not accessible")
            urlopen.side_effect = self.http_error(403, b"{}", {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"})
            with self.assertRaises(github.GitHubError) as cm:
                gh._request("GET", "/x")
            self.assertIn("quota used up", str(cm.exception))

    def test_gh_problems_say_what_to_do(self):
        import subprocess
        with mock.patch.object(server.acc.subprocess, "check_output", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "sudo apt install gh"):
                server.acc.gh_token("github.com", "me")
        with mock.patch.object(server.acc.subprocess, "check_output", side_effect=subprocess.CalledProcessError(1, "gh")):
            with self.assertRaisesRegex(RuntimeError, "gh auth login"):
                server.acc.gh_token("github.com", "me")

    def test_token_only_goes_to_the_account_api(self):
        from github import GitHub
        gh = GitHub("github.com", lambda: "secret")
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(RuntimeError):
                gh._request("GET", "https://evil.example/notifications?page=2")
            with self.assertRaises(RuntimeError):
                gh._request("GET", "https://api.github.com.evil.example/x")
            urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
