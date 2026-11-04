#!/usr/bin/env bash
# Install the PR Inbox launcher in the GNOME app grid (and optionally autostart the backend).
#   ./install.sh              check the requirements, then add "PR Inbox" to the applications menu / dock
#   ./install.sh --autostart  also run the backend at login (desktop notifications with the window closed)
#   ./install.sh --check      only check the requirements and say how to fix what is missing
#   ./install.sh --uninstall  remove the launcher and the autostart entry
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ID=com.github.javiergalilea.PrInbox
APPS="$HOME/.local/share/applications"
AUTOSTART="$HOME/.config/autostart"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$APPS/$APP_ID.desktop" "$AUTOSTART/pr-inbox-backend.desktop"
  update-desktop-database "$APPS" 2>/dev/null || true
  echo "Uninstalled. Your data is still in ~/.local/share/pr-inbox (delete that folder to remove it too)."
  exit 0
fi

# --- requirements -----------------------------------------------------------
# Required problems stop the install; optional ones only warn. Missing apt packages are gathered into one command.
missing_pkgs=() errors=0
ok()   { echo "  ✓ $1"; }
warn() { echo "  ! $1"; }
fail() { echo "  ✗ $1"; errors=$((errors + 1)); }
gi_has() { python3 -c "import gi; gi.require_version('$1', '$2'); from gi.repository import $1" 2>/dev/null; }

check_requirements() {
  echo "Checking requirements…"
  if ! command -v python3 >/dev/null; then
    fail "Python 3 is not installed"; missing_pkgs+=(python3)
  elif ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    fail "Python 3.10 or newer is needed (found $(python3 -V 2>&1)): use Ubuntu 22.04 or newer"
  else
    ok "$(python3 -V)"
  fi

  if gi_has Gtk 3.0 && gi_has WebKit2 4.1; then
    ok "WebKitGTK window (Gtk 3, WebKit2 4.1)"
  elif command -v google-chrome >/dev/null || command -v chromium >/dev/null || command -v chromium-browser >/dev/null; then
    warn "WebKitGTK is missing: the app will open in a Chrome window instead (install it for the native window)"
    missing_pkgs+=(python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1)
  else
    fail "WebKitGTK is missing and there is no Chrome/Chromium to fall back to"
    missing_pkgs+=(python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1)
  fi

  if gi_has Secret 1; then
    ok "GNOME keyring support (for personal tokens)"
  else
    warn "GNOME keyring support is missing: only needed to add accounts with a personal token"
    missing_pkgs+=(gir1.2-secret-1)
  fi

  if command -v notify-send >/dev/null; then
    ok "Desktop notifications (notify-send)"
  else
    warn "notify-send is missing: the app works, but without desktop notifications"
    missing_pkgs+=(libnotify-bin)
  fi

  if ! command -v gh >/dev/null; then
    warn "The GitHub CLI (gh) is not installed: it is the easiest way to sign in"
    missing_pkgs+=(gh)
    gh_hint="then run: gh auth login"
  elif ! gh auth status >/dev/null 2>&1; then
    warn "gh is installed but not signed in"
    gh_hint="run: gh auth login"
  else
    ok "gh is signed in ($(gh api user --jq .login 2>/dev/null || echo "account found"))"
  fi

  if ((${#missing_pkgs[@]})); then
    echo
    echo "To install what is missing:"
    echo "  sudo apt install ${missing_pkgs[*]}"
  fi
  if [[ -n "${gh_hint:-}" ]]; then
    echo "To connect your GitHub account, $gh_hint"
    echo "  (or add a personal token later in the app: Settings → Add account)"
  fi
  echo
}

check_requirements
if [[ "${1:-}" == "--check" ]]; then
  exit $((errors > 0))
fi
if ((errors)); then
  echo "Fix the problems marked ✗ and run ./install.sh again." >&2
  exit 1
fi

# The path is written into .desktop Exec lines (and a sh -c command): refuse characters that would need escaping.
if [[ "$DIR" =~ [\"\'\`\$\\%] ]]; then
  echo "Move PR Inbox to a folder whose path has no quotes, \$, %, \\ or backticks." >&2
  exit 1
fi

chmod +x "$DIR/pr-inbox" "$DIR/app.py" "$DIR/server.py"
mkdir -p "$APPS"
cat >"$APPS/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=PR Inbox
Comment=GitHub pull request notifications, grouped and prioritised
Exec="$DIR/pr-inbox"
Icon=$DIR/static/icon.svg
Terminal=false
Categories=Development;
StartupWMClass=$APP_ID
StartupNotify=true
EOF
update-desktop-database "$APPS" 2>/dev/null || true
echo "Installed: search for \"PR Inbox\" in Activities and pin it to the dock."

if [[ "${1:-}" == "--autostart" ]]; then
  mkdir -p "$AUTOSTART"
  cat >"$AUTOSTART/pr-inbox-backend.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=PR Inbox (backend)
Exec=sh -c 'umask 077; python3 "$DIR/server.py" >>"\${XDG_STATE_HOME:-\$HOME/.local/state}/pr-inbox.log" 2>&1'
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOF
  echo "Backend will start at login. After updating PR Inbox, log out and back in so it runs the new version."
fi
