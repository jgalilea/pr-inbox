"""Account sources: the `gh` CLI's logged-in accounts, or personal access tokens kept in the GNOME keyring."""
import json
import subprocess

KEYRING_SCHEMA = "com.github.javiergalilea.PrInbox"


def gh_accounts():
    """Accounts logged in with `gh auth login`: [{host, login, active}]."""
    try:
        out = subprocess.run(["gh", "auth", "status", "--json", "hosts"], capture_output=True, text=True, timeout=20).stdout
        hosts = json.loads(out or "{}").get("hosts", {})
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return [{"host": host, "login": a["login"], "active": a.get("active", False)}
            for host, accs in hosts.items() for a in accs if a.get("state") == "success"]


def gh_token(host, login):
    try:
        return subprocess.check_output(["gh", "auth", "token", "--hostname", host, "--user", login],
                                       text=True, timeout=20, stderr=subprocess.DEVNULL).strip()
    except FileNotFoundError:
        raise RuntimeError("The GitHub CLI (gh) is not installed. Install it with `sudo apt install gh`, "
                           "then run `gh auth login`.") from None
    except subprocess.CalledProcessError:
        raise RuntimeError(f"gh has no session for {login} on {host}. Run `gh auth login --hostname {host}` "
                           "in a terminal; the app picks it up on the next sync.") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("gh did not answer in 20 s (is it waiting for the keyring to unlock?).") from None


def _secret():
    try:
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret
    except (ImportError, ValueError):
        raise RuntimeError("GNOME keyring support is missing: install it with `sudo apt install gir1.2-secret-1`.") from None
    schema = Secret.Schema.new(KEYRING_SCHEMA, Secret.SchemaFlags.NONE, {"account": Secret.SchemaAttributeType.STRING})
    return Secret, schema


def keyring_store(account_id, token):
    Secret, schema = _secret()
    ok = Secret.password_store_sync(schema, {"account": account_id}, Secret.COLLECTION_DEFAULT,
                                    f"PR Inbox: {account_id}", token, None)
    if not ok:
        raise RuntimeError("Could not store the token in the GNOME keyring")


def keyring_load(account_id):
    Secret, schema = _secret()
    token = Secret.password_lookup_sync(schema, {"account": account_id}, None)
    if not token:
        raise RuntimeError("Token not found in the GNOME keyring; add the account again")
    return token


def keyring_delete(account_id):
    Secret, schema = _secret()
    Secret.password_clear_sync(schema, {"account": account_id}, None)
