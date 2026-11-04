#!/usr/bin/env python3
"""PR Inbox desktop window (WebKitGTK). Starts the backend in-process unless one is already running."""
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import DATA_DIR, VERSION, secret_token  # noqa: E402

APP_ID = "com.github.javiergalilea.PrInbox"
PORT = int(os.environ.get("PR_INBOX_PORT", 8787))
URL = f"http://127.0.0.1:{PORT}/"


def tell(summary, body):
    """Problems before the window exists: desktop notification (if possible) plus the log."""
    print(f"{summary}: {body}", file=sys.stderr, flush=True)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "PR Inbox", "--", summary, body])


def backend_running():
    """True if our own backend answers on PORT. Exits if something else holds the port: the window would
    hand it the secret and show its pages."""
    nonce = secrets.token_hex(16)
    try:
        with urllib.request.urlopen(f"{URL}api/ping?n={nonce}", timeout=1) as r:
            ping = json.load(r)
    except Exception:
        return False
    if hmac.compare_digest(str(ping.get("proof")), hmac.new(secret_token().encode(), nonce.encode(), "sha256").hexdigest()):
        if ping.get("version") != VERSION:  # e.g. the autostart backend still runs the code from before an update
            tell("PR Inbox: restart the backend", f"The running backend is version {ping.get('version') or 'unknown'} "
                 f"and the app is {VERSION}. Log out and back in (or stop the backend process) to use the update.")
        return True
    msg = (f"Port {PORT} is taken by another program, another user's PR Inbox or an outdated PR Inbox backend. "
           "Stop it, or start PR Inbox with PR_INBOX_PORT set to a free port.")
    tell("PR Inbox can't start", msg)
    sys.exit(msg)


def ensure_backend():
    if not backend_running():
        import server
        server.serve(PORT)


def run_webkit():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2

    GLib.set_prgname(APP_ID)
    GLib.set_application_name("PR Inbox")
    here = os.path.dirname(os.path.abspath(__file__))

    def open_external(uri):
        Gio.AppInfo.launch_default_for_uri(uri, None)

    def on_decide(view, decision, kind):
        # Anything that isn't the app itself (GitHub links, CI links…) opens in the default browser.
        if kind in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION, WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri()
            if not uri.startswith(URL):
                decision.ignore()
                if uri.startswith(("https://", "http://")):  # never hand file:, custom-scheme… links to the desktop
                    open_external(uri)
                return True
        return False

    def follow_gnome_theme():
        # GTK3/WebKit don't see libadwaita's color-scheme by themselves; mirror it so prefers-color-scheme works.
        source = Gio.SettingsSchemaSource.get_default()
        schema = source and source.lookup("org.gnome.desktop.interface", True)
        if not schema or not schema.has_key("color-scheme"):
            return
        gsettings = Gio.Settings.new("org.gnome.desktop.interface")

        def sync(*_):
            dark = gsettings.get_string("color-scheme") == "prefer-dark"
            Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", dark)

        gsettings.connect("changed::color-scheme", sync)
        sync()
        return gsettings  # keep a reference so the signal stays connected

    def watch_badge():
        """Number of PRs needing action on the dock icon (Unity LauncherEntry API, supported by Ubuntu Dock)."""
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        last = [None]

        def emit(count):
            if count != last[0]:
                last[0] = count
                bus.emit_signal(None, "/com/github/javiergalilea/PrInbox", "com.canonical.Unity.LauncherEntry", "Update",
                                GLib.Variant("(sa{sv})", (f"application://{APP_ID}.desktop", {
                                    "count": GLib.Variant("x", count), "count-visible": GLib.Variant("b", count > 0)})))
            return False

        def fetch():
            try:
                req = urllib.request.Request(URL + "api/badge", headers={"X-PR-Inbox-Token": secret_token()})
                with urllib.request.urlopen(req, timeout=5) as r:
                    GLib.idle_add(emit, int(json.load(r)["count"]))
            except Exception:
                pass

        def tick():
            threading.Thread(target=fetch, daemon=True).start()
            return True

        tick()
        GLib.timeout_add_seconds(5, tick)

    def remember_geometry(win):
        """Restore the last size / maximized state (and position on X11: Wayland doesn't let apps place windows)."""
        path = DATA_DIR / "window.json"
        try:
            geo = json.loads(path.read_text())
        except (OSError, ValueError):
            geo = {}
        x11 = "X11" in type(Gdk.Display.get_default()).__name__
        win.set_default_size(geo.get("width", 1400), geo.get("height", 860))
        if x11 and "x" in geo:
            win.move(geo["x"], geo["y"])
        if geo.get("maximized"):
            win.maximize()

        def track(*_):
            # Keep the normal size while maximized, so un-maximizing after a restart goes back to it.
            if not win.is_maximized():
                geo["width"], geo["height"] = win.get_size()
                if x11:
                    geo["x"], geo["y"] = win.get_position()
            geo["maximized"] = win.is_maximized()

        def save(*_):
            track()
            try:
                path.write_text(json.dumps(geo))
            except OSError:
                pass

        win.connect("configure-event", track)
        win.connect("window-state-event", track)
        win.connect("delete-event", save)

    def on_activate(app):
        if app.get_windows():
            app.get_windows()[0].present()
            return
        win = Gtk.ApplicationWindow(application=app, title="PR Inbox")
        remember_geometry(win)
        win.set_icon_from_file(os.path.join(here, "static", "icon.svg"))
        ctx = WebKit2.WebContext.get_default()
        ctx.set_cache_model(WebKit2.CacheModel.DOCUMENT_VIEWER)
        view = WebKit2.WebView.new_with_context(ctx)
        view.get_settings().set_javascript_can_access_clipboard(True)  # for the 🔗 copy-link button
        view.connect("decide-policy", on_decide)
        view.load_uri(f"{URL}auth?k={secret_token()}")  # the backend swaps it for an HttpOnly cookie
        win.add(view)
        win.show_all()

    app = Gtk.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
    app.connect("activate", on_activate)
    app.connect("startup", lambda a: (setattr(a, "_theme_watch", follow_gnome_theme()), watch_badge()))
    # Only the primary instance starts the backend; a second launch just raises the window.
    app.register(None)
    if not app.get_is_remote():
        ensure_backend()
    return app.run([])


def run_chrome():
    ensure_backend()
    browser = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not browser:
        msg = ("The window needs WebKitGTK: install it with `sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1` "
               "(or have Chrome/Chromium). `./install.sh --check` lists everything that is missing.")
        tell("PR Inbox can't open its window", msg)
        sys.exit(msg)
    data = os.path.expanduser("~/.local/share/pr-inbox/chrome")
    # The secret can't go on the command line (any local user can read it with `ps`): a private page redirects.
    opener = DATA_DIR / "open.html"
    fd = os.open(opener, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f'<meta http-equiv="refresh" content="0;url={URL}auth?k={secret_token()}">')
    # Own profile => own process, so --class is honoured and the dock groups the window correctly.
    return subprocess.call([browser, f"--app={opener.as_uri()}", f"--class={APP_ID}", f"--user-data-dir={data}",
                            "--no-first-run", "--no-default-browser-check"])


if __name__ == "__main__":
    if os.environ.get("PR_INBOX_BROWSER") == "chrome":
        sys.exit(run_chrome())
    try:
        sys.exit(run_webkit())
    except (ImportError, ValueError) as e:
        print(f"WebKitGTK unavailable ({e}); falling back to Chrome", file=sys.stderr)
        sys.exit(run_chrome())
