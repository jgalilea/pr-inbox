"""Minimal GitHub REST + GraphQL client (stdlib only). One instance per account."""
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime

TRANSIENT = {502, 503, 504}  # GitHub hiccups: retried, and only reported if they persist
RETRY_DELAYS = (2, 6)


class GitHubError(RuntimeError):
    """A failed GitHub call, with a message meant for the user (what happened and what to do)."""

    def __init__(self, message, transient=False):
        super().__init__(message)
        self.transient = transient


def _api_message(raw):
    """GitHub's own error message from a JSON body; None for HTML error pages."""
    try:
        return (json.loads(raw) or {}).get("message")
    except (ValueError, AttributeError):
        return None


def endpoints(host):
    """REST and GraphQL base URLs for github.com or a GitHub Enterprise Server host."""
    if host in ("github.com", "api.github.com"):
        return "https://api.github.com", "https://api.github.com/graphql"
    return f"https://{host}/api/v3", f"https://{host}/api/graphql"


class GitHub:
    def __init__(self, host="github.com", token_provider=None):
        """token_provider: callable returning the current token (re-read on 401, e.g. after `gh auth refresh`)."""
        self.host = host
        self.api, self.graphql_url = endpoints(host)
        self.token_provider = token_provider
        self.token = token_provider()
        self.rate = {}  # resource -> {"remaining", "limit", "used", "reset"}
        self.spent = 0  # GraphQL points spent by this client, as GitHub reports each query's cost
        self.scopes = None

    def _request(self, method, url, body=None, headers=None, _retry=True):
        if not url.startswith("http"):
            url = self.api + url
        elif not url.startswith((self.api + "/", self.graphql_url)):
            # Never send the token anywhere but this account's API (e.g. a pagination Link pointing elsewhere).
            raise GitHubError(f"Refusing to send the token to {url.split('?')[0]}")
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        for attempt in range(len(RETRY_DELAYS) + 1):
            last = attempt == len(RETRY_DELAYS)
            try:
                with urllib.request.urlopen(req, data=data, timeout=60) as resp:
                    raw = resp.read()
                    self._track_rate(resp.headers)
                    return resp.status, resp.headers, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as e:
                self._track_rate(e.headers)
                if e.code == 304:
                    return 304, e.headers, None
                if e.code in TRANSIENT and not last:
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                if e.code == 401 and _retry:  # token rotated by `gh auth refresh/login`: pick up the new one
                    self.token = self.token_provider()
                    return self._request(method, url, body, headers, _retry=False)
                raise self._http_error(e) from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if not last:
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                reason = getattr(e, "reason", None) or e
                raise GitHubError(f"Cannot reach {self.host} ({reason}). Check your network or VPN; "
                                  "the app retries on its own.", transient=True) from None

    def _http_error(self, e):
        msg = _api_message(e.read()) or e.reason
        if e.code == 401:
            return GitHubError("GitHub rejected the token (expired or revoked). If the account comes from gh, run "
                               "`gh auth login` in a terminal; with a personal token, create a new one and add the account again.")
        if e.code in TRANSIENT:
            return GitHubError(f"GitHub is temporarily unavailable ({e.code}). The app keeps retrying.", transient=True)
        if e.code in (403, 429) and e.headers and e.headers.get("X-RateLimit-Remaining") == "0":
            reset = datetime.fromtimestamp(int(e.headers.get("X-RateLimit-Reset", 0))).strftime("%H:%M")
            return GitHubError(f"GitHub API quota used up for this account; it resets at {reset}. "
                               "A longer sync interval (Settings → Advanced) spends less.", transient=True)
        if e.code == 403 and "SAML" in str(msg):
            return GitHubError(f"{msg} Authorize the token for the organization: with gh, run `gh auth refresh`; "
                               "with a personal token, use \"Configure SSO\" on github.com/settings/tokens.")
        return GitHubError(f"GitHub answered {e.code}: {msg}")

    def _track_rate(self, hdrs):
        if not hdrs:
            return
        if hdrs.get("X-OAuth-Scopes") is not None:
            self.scopes = [x.strip() for x in hdrs["X-OAuth-Scopes"].split(",") if x.strip()]
        if hdrs.get("X-RateLimit-Resource"):
            self.rate[hdrs["X-RateLimit-Resource"]] = {
                "remaining": int(hdrs.get("X-RateLimit-Remaining", 0)),
                "limit": int(hdrs.get("X-RateLimit-Limit", 0)),
                "used": int(hdrs.get("X-RateLimit-Used", 0)),
                "reset": int(hdrs.get("X-RateLimit-Reset", 0))}

    def rate_limit(self):
        """Current quotas for every API (this call itself is free)."""
        _, _, data = self._request("GET", "/rate_limit")
        for name in ("core", "graphql", "search"):
            r = data["resources"].get(name)
            if r:
                self.rate[name] = {k: r[k] for k in ("remaining", "limit", "used", "reset")}
        return self.rate

    # --- REST -------------------------------------------------------------

    def notifications(self, since, last_modified=None):
        """Returns (threads, last_modified, poll_interval). threads is None on 304."""
        url = f"/notifications?participating=true&all=true&per_page=50&since={since}"
        headers = {"If-Modified-Since": last_modified} if last_modified else {}
        status, hdrs, data = self._request("GET", url, headers=headers)
        poll = int(hdrs.get("X-Poll-Interval", 60))
        if status == 304:
            return None, last_modified, poll
        threads, new_lm = list(data), hdrs.get("Last-Modified")
        nxt = _next_link(hdrs.get("Link"))
        while nxt:
            _, hdrs, data = self._request("GET", nxt)
            threads.extend(data)
            nxt = _next_link(hdrs.get("Link"))
        return threads, new_lm, poll

    def mark_thread_read(self, thread_id):
        self._request("PATCH", f"/notifications/threads/{thread_id}")

    def set_thread_ignored(self, thread_id, ignored):
        self._request("PUT", f"/notifications/threads/{thread_id}/subscription", {"ignored": ignored})

    # --- GraphQL ----------------------------------------------------------

    def graphql(self, query, variables=None):
        # Ask for the query's own cost: the rate-limit headers come from different servers and jump around.
        brace = query.index("{") + 1
        query = query[:brace] + " rateLimit { cost }" + query[brace:]
        _, _, data = self._request("POST", self.graphql_url, {"query": query, "variables": variables or {}})
        self.spent += ((data.get("data") or {}).get("rateLimit") or {}).get("cost") or 0
        if data.get("errors") and not data.get("data"):
            raise GitHubError("GitHub GraphQL error: " + "; ".join(str(e.get("message", e)) for e in data["errors"]))
        return data["data"]

    def viewer(self):
        return self.graphql("{ viewer { login } }")["viewer"]["login"]

    def search_prs(self, q):
        query = """query($q:String!){ search(query:$q, type:ISSUE, first:100){
            nodes{ ... on PullRequest { number repository { nameWithOwner } } } } }"""
        nodes = self.graphql(query, {"q": q})["search"]["nodes"]
        return [f"{n['repository']['nameWithOwner']}#{n['number']}" for n in nodes if n]

    def pull_requests(self, keys, batch=10, fragment=None):
        """keys: ['owner/repo#123', ...] -> {key: pr_node or None}. fragment: PR_FRAGMENT (default) or LIGHT_FRAGMENT."""
        out = {}
        for i in range(0, len(keys), batch):
            chunk = keys[i:i + batch]
            parts = []
            for j, key in enumerate(chunk):
                repo, num = key.rsplit("#", 1)
                owner, name = repo.split("/", 1)
                parts.append(f'p{j}: repository(owner:{json.dumps(owner)}, name:{json.dumps(name)})'
                             f'{{ pullRequest(number:{int(num)}) {{ ...P }} }}')
            data = self.graphql("query{" + "\n".join(parts) + "}" + (fragment or PR_FRAGMENT))
            for j, key in enumerate(chunk):
                repo = data.get(f"p{j}")
                out[key] = repo and repo.get("pullRequest")
        return out


    def my_teams(self, login):
        """Teams the user belongs to, with their members: [{org, slug, name, size, members}]"""
        query = """query($login:String!){ viewer { organizations(first: 30) { nodes { login
            teams(first: 50, userLogins: [$login]) { nodes { slug name
              members(first: 100) { totalCount nodes { login } } } } } } } }"""
        orgs = self.graphql(query, {"login": login})["viewer"]["organizations"]["nodes"]
        return [{"org": o["login"], "slug": t["slug"], "name": t["name"], "size": t["members"]["totalCount"],
                 "members": [m["login"] for m in t["members"]["nodes"]]}
                for o in orgs for t in o["teams"]["nodes"]]


def _next_link(link):
    m = link and re.search(r'<([^>]+)>;\s*rel="next"', link)
    return m.group(1) if m else None


_ACTOR = "author { __typename login avatarUrl }"
_COMMENT = f"{_ACTOR} body createdAt url"

PR_FRAGMENT = f"""
fragment P on PullRequest {{
  number title url state isDraft merged updatedAt createdAt body headRefOid
  mergeable mergeStateStatus additions deletions changedFiles
  {_ACTOR}
  repository {{ nameWithOwner }}
  reviewDecision
  latestReviews(first: 30) {{ nodes {{ {_ACTOR} state submittedAt url commit {{ oid }} }} }}
  reviewRequests(first: 30) {{ nodes {{ requestedReviewer {{
    __typename ... on User {{ login avatarUrl }} ... on Bot {{ login avatarUrl }} ... on Team {{ name slug }} }} }} }}
  reviews(last: 50) {{ nodes {{ {_COMMENT} state }} }}
  comments(last: 60) {{ nodes {{ {_COMMENT} }} }}
  reviewThreads(last: 40) {{ nodes {{ isResolved isOutdated path comments(first: 30) {{ nodes {{ {_COMMENT} }} }} }} }}
  timelineItems(itemTypes: [REVIEW_REQUESTED_EVENT], last: 30) {{ nodes {{ ... on ReviewRequestedEvent {{ createdAt
    requestedReviewer {{ ... on User {{ login }} ... on Bot {{ login }} ... on Team {{ slug }} }} }} }} }}
  commits(last: 1) {{ nodes {{ commit {{ committedDate statusCheckRollup {{ state
    contexts(first: 100) {{ totalCount nodes {{ __typename
      ... on CheckRun {{ name conclusion status detailsUrl startedAt completedAt
        checkSuite {{ workflowRun {{ runNumber workflow {{ name }} }} }} }}
      ... on StatusContext {{ context state targetUrl }} }} }} }} }} }} }}
}}
"""

# Just what tells whether a PR changed since we cached it (see server.fingerprint): about 1 point for 30 PRs.
# Comments, reviews, requests, title or draft changes bump updatedAt; pushes change the head; CI and the base
# branch show in the checks and the merge state.
LIGHT_FRAGMENT = """
fragment P on PullRequest {
  updatedAt headRefOid state isDraft mergeable mergeStateStatus reviewDecision
  commits(last: 1) { nodes { commit { statusCheckRollup { state contexts(first: 100) { totalCount nodes { __typename
    ... on CheckRun { status conclusion } ... on StatusContext { state } } } } } } }
}
"""
