#!/usr/bin/env bash
# Rerun installer for Ubuntu (desktop or server). No Docker needed.
#
#   Usually started by the one-line command in README.md, or by "rerun update".
#   From a downloaded copy: open a terminal in the rerun folder and run   bash install.sh
#
# On a desktop, the questions pop up as windows. Run it again any time to update Rerun
# or change your answers; your channels and settings are kept, and if a new version
# won't start, the previous one is put back automatically.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
SRC="$(pwd)"

APP_DIR=/opt/rerun
DATA_DIR=/var/lib/rerun
CONF=/etc/rerun.conf
UNIT=/etc/systemd/system/rerun.service
PORT=8410
MEDIA_PATHS=""
RUN_USER=""
# shellcheck disable=SC1090
[ -r "$CONF" ] && . "$CONF"

ME="$(id -un)"
[ "$ME" = "root" ] && [ -n "${SUDO_USER:-}" ] && ME="$SUDO_USER"
if [ "$ME" = "root" ]; then
  echo "Please run this as your normal user, not root:  bash install.sh   (it asks for your password when it needs it)" >&2
  exit 1
fi
[ -n "$RUN_USER" ] || RUN_USER="$ME"
RUN_GROUP="$(id -gn "$RUN_USER")"
USER_HOME="$(getent passwd "$ME" | cut -d: -f6)"

# ---------------------------------------------------------------- talking to the person
GUI=0
if command -v zenity >/dev/null && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then GUI=1; fi
TITLE="Rerun installer"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die()  {
  printf '\033[31m✗ %s\033[0m\n' "$*" >&2
  [ "$GUI" = 1 ] && zenity --error --title="$TITLE" --width=420 --text="$*" 2>/dev/null || true
  exit 1
}
ask_yes() {  # ask_yes "question"
  if [ "$GUI" = 1 ]; then zenity --question --title="$TITLE" --width=440 --text="$1" 2>/dev/null; return; fi
  local a; read -r -p "$1 (y/n) [y] " a </dev/tty || true; [[ "${a:-y}" =~ ^[Yy] ]]
}
ask_text() {  # ask_text "question" "default"
  if [ "$GUI" = 1 ]; then zenity --entry --title="$TITLE" --width=440 --text="$1" --entry-text="$2" 2>/dev/null; return; fi
  local a; read -r -p "$1 [$2] " a </dev/tty || true; printf '%s' "${a:-$2}"
}

# A graphical password prompt for sudo when there's a desktop.
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null || die "Please install sudo, or run this as root."
  if [ "$GUI" = 1 ]; then
    ASKPASS="$(mktemp)"
    printf '#!/bin/sh\nzenity --password --title="Rerun installer needs your password"\n' >"$ASKPASS"
    chmod 700 "$ASKPASS"
    export SUDO_ASKPASS="$ASKPASS"
    trap 'rm -f "$ASKPASS"' EXIT
    SUDO="sudo -A"
  else
    SUDO="sudo"
  fi
  $SUDO -v || die "Rerun needs administrator rights to install."
else
  SUDO=""
fi

[ -r /etc/os-release ] && . /etc/os-release
command -v apt-get >/dev/null || die "This installer is for Ubuntu (or Debian)."

say "Rerun installer"
info "Rerun turns your Plex library into live TV channels."
if [ ! -d "$APP_DIR" ]; then
  ask_yes "Install Rerun on this computer?

It runs quietly in the background, starts when the computer starts, and restarts itself if anything goes wrong." || exit 0
fi

# ---------------------------------------------------------------- 1. packages
say "1. Installing what Rerun needs (Python and ffmpeg)…"
PKGS="python3 python3-venv ffmpeg fonts-dejavu-core ca-certificates"
[ -d /dev/dri ] && PKGS="$PKGS va-driver-all vainfo"
# Intel graphics (Quick Sync): the full Intel media driver can encode on every generation;
# the free one can't on some. It lives in "multiverse", so fall back quietly if that's turned off.
INTEL_GPU=0
grep -qs 0x8086 /sys/class/drm/renderD*/device/vendor && INTEL_GPU=1
run_apt() {
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq
  # shellcheck disable=SC2086
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $PKGS
  if [ "$INTEL_GPU" = 1 ]; then
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq intel-media-va-driver-non-free \
      || $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq intel-media-va-driver || true
  fi
}
if [ "$GUI" = 1 ]; then
  run_apt 2>&1 | zenity --progress --pulsate --auto-close --no-cancel --title="$TITLE" --width=420 \
    --text="Installing Python and ffmpeg… this can take a few minutes." 2>/dev/null || true
  for p in python3 ffmpeg; do command -v "$p" >/dev/null || die "Couldn't install $p. Check your internet connection and try again."; done
else
  run_apt
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Rerun needs Python 3.10 or newer (Ubuntu 22.04 or later)."

# ---------------------------------------------------------------- 2. media folders
say "2. Your media folders"
declare -a MEDIA=()
if [ -n "$MEDIA_PATHS" ]; then IFS=':' read -r -a MEDIA <<<"$MEDIA_PATHS"; fi
if [ ${#MEDIA[@]} -gt 0 ] && ask_yes "Keep using these media folders?

$(printf '%s\n' "${MEDIA[@]}")"; then
  :
else
  MEDIA=()
  # Look at Plex's own library list to suggest the right folders.
  PLEXDB="/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"
  FOUND=""
  if $SUDO test -r "$PLEXDB"; then
    FOUND="$($SUDO python3 - "$PLEXDB" <<'PY' 2>/dev/null || true
import os, sqlite3, sys, urllib.parse
db = sqlite3.connect("file:" + urllib.parse.quote(sys.argv[1]) + "?mode=ro&immutable=1", uri=True)
roots = sorted({r[0].rstrip("/") for r in db.execute("select root_path from section_locations") if r[0]})
parents = sorted({os.path.dirname(r) if os.path.dirname(r) not in ("", "/") else r for r in roots})
print("\n".join(p for p in parents if os.path.isdir(p)))
PY
)"
  fi
  if [ -n "$FOUND" ]; then
    if [ "$GUI" = 1 ]; then
      ARGS=(); while IFS= read -r p; do ARGS+=(TRUE "$p"); done <<<"$FOUND"
      PICKED="$(zenity --list --checklist --title="$TITLE" --width=560 --height=320 \
        --text="These are the folders your Plex libraries use. Rerun only ever reads them." \
        --column="Use" --column="Folder" "${ARGS[@]}" --separator=$'\n' 2>/dev/null || true)"
      while IFS= read -r p; do [ -n "$p" ] && MEDIA+=("$p"); done <<<"$PICKED"
    else
      info "Found these from your Plex libraries:"; while IFS= read -r p; do info "  $p"; done <<<"$FOUND"
      if ask_yes "Use these?"; then while IFS= read -r p; do MEDIA+=("$p"); done <<<"$FOUND"; fi
    fi
  fi
  while :; do
    if [ ${#MEDIA[@]} -gt 0 ]; then
      ask_yes "Rerun will read:

$(printf '%s\n' "${MEDIA[@]}")

Add another folder (for example where your commercials or music videos are)?" || break
    fi
    if [ "$GUI" = 1 ]; then
      p="$(zenity --file-selection --directory --title="Choose a folder with your videos" --filename="$USER_HOME/" 2>/dev/null || true)"
      [ -z "$p" ] && { [ ${#MEDIA[@]} -gt 0 ] && break; die "Rerun needs at least one media folder."; }
    else
      read -r -p "  Folder path (e.g. /mnt/media): " p </dev/tty || true
      [ -z "$p" ] && { [ ${#MEDIA[@]} -gt 0 ] && break; continue; }
    fi
    p="$(realpath -m "$p")"
    [[ "$p" == *:* ]] && { info "Folder names with a colon (:) aren't supported."; continue; }
    [ -d "$p" ] || { info "$p doesn't exist."; continue; }
    MEDIA+=("$p")
  done
fi
for p in "${MEDIA[@]}"; do [ -d "$p" ] || die "The folder $p doesn't exist any more."; done
MEDIA_JOINED="$(IFS=:; echo "${MEDIA[*]}")"

# ---------------------------------------------------------------- 3. port
if command -v ss >/dev/null && ss -ltn "sport = :$PORT" | grep -q LISTEN && ! systemctl is-active --quiet rerun; then
  PORT="$(ask_text "Port $PORT is already in use by something else. Which port should Rerun use?" 8411)"
fi

# ---------------------------------------------------------------- 4. install the app (keeping the old one for rollback)
say "3. Installing Rerun…"
$SUDO mkdir -p "$APP_DIR" "$DATA_DIR"
$SUDO chown "$RUN_USER:$RUN_GROUP" "$DATA_DIR"
$SUDO chmod 700 "$DATA_DIR"
WAS_RUNNING=0
systemctl is-active --quiet rerun && WAS_RUNNING=1
if [ -f "$DATA_DIR/config.json" ]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  $SUDO mkdir -p "$DATA_DIR/backups"
  $SUDO tar -czf "$DATA_DIR/backups/before-install-$STAMP.tar.gz" -C "$DATA_DIR" config.json secrets.json 2>/dev/null || true
fi
$SUDO rm -rf "$APP_DIR/app.new"
$SUDO cp -r "$SRC/app" "$APP_DIR/app.new"
$SUDO cp "$SRC/requirements.txt" "$APP_DIR/requirements.txt"
$SUDO find "$APP_DIR/app.new" -name __pycache__ -prune -exec rm -rf {} +
[ -d "$APP_DIR/venv" ] || $SUDO python3 -m venv "$APP_DIR/venv"
install_reqs() { $SUDO "$APP_DIR/venv/bin/pip" install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"; }
if [ "$GUI" = 1 ]; then
  install_reqs 2>&1 | zenity --progress --pulsate --auto-close --no-cancel --title="$TITLE" --width=420 --text="Setting up Rerun…" 2>/dev/null || true
  $SUDO "$APP_DIR/venv/bin/python" -c "import fastapi, uvicorn, httpx, PIL" || die "Couldn't install Rerun's parts. Check your internet connection and try again."
else
  install_reqs
fi
[ "$WAS_RUNNING" = 1 ] && $SUDO systemctl stop rerun
$SUDO rm -rf "$APP_DIR/app.prev"
[ -d "$APP_DIR/app" ] && $SUDO mv "$APP_DIR/app" "$APP_DIR/app.prev"
$SUDO mv "$APP_DIR/app.new" "$APP_DIR/app"
$SUDO install -m 755 "$SRC/rerun.sh" /usr/local/bin/rerun

# ---------------------------------------------------------------- 5. the background service
GROUPS_LINE=""
for g in video render; do getent group "$g" >/dev/null && GROUPS_LINE="$GROUPS_LINE $g"; done
$SUDO tee "$CONF" >/dev/null <<EOF
# Your answers from install.sh (used when you run it again).
PORT=$PORT
MEDIA_PATHS="$MEDIA_JOINED"
RUN_USER=$RUN_USER
EOF
$SUDO tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Rerun live TV channels
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
${GROUPS_LINE:+SupplementaryGroups=$GROUPS_LINE}
WorkingDirectory=$APP_DIR
Environment=RERUN_DATA=$DATA_DIR
Environment=RERUN_PORT=$PORT
Environment="RERUN_MEDIA_ROOTS=$MEDIA_JOINED"
ExecStart=$APP_DIR/venv/bin/python -m app
# Comes back by itself after crashes; starts when the computer starts.
Restart=always
RestartSec=5
TimeoutStopSec=20
# Locked down: it can read your media but can only write to its own data folder.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=$DATA_DIR
ProtectHome=read-only
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
EOF
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now rerun >/dev/null 2>&1
$SUDO systemctl restart rerun

healthy() {
  for _ in $(seq 1 30); do
    "$APP_DIR/venv/bin/python" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/health', timeout=3)" 2>/dev/null && return 0
    sleep 2
  done
  return 1
}
if ! healthy; then
  if [ -d "$APP_DIR/app.prev" ]; then
    $SUDO rm -rf "$APP_DIR/app.failed"
    $SUDO mv "$APP_DIR/app" "$APP_DIR/app.failed"
    $SUDO mv "$APP_DIR/app.prev" "$APP_DIR/app"
    $SUDO systemctl restart rerun
    healthy && die "The new version didn't start, so Rerun went back to the version you had. Nothing changed. Run 'rerun logs' to see why."
  fi
  die "Rerun didn't start. Run 'rerun logs' in a terminal to see why."
fi

# ---------------------------------------------------------------- 6. menu icon and firewall
APPS="$USER_HOME/.local/share/applications"
if [ -d "$USER_HOME" ]; then
  if [ "$(id -u)" -eq 0 ]; then $SUDO -u "$ME" mkdir -p "$APPS"; else mkdir -p "$APPS"; fi
  cat >"/tmp/rerun.desktop.$$" <<EOF
[Desktop Entry]
Type=Application
Name=Rerun TV
Comment=Your own live TV channels
Exec=xdg-open http://localhost:$PORT
Icon=$APP_DIR/app/static/icon.svg
Terminal=false
Categories=AudioVideo;Video;
EOF
  $SUDO install -o "$ME" -m 644 "/tmp/rerun.desktop.$$" "$APPS/rerun.desktop"
  rm -f "/tmp/rerun.desktop.$$"
fi

if command -v ufw >/dev/null && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
  NET="$(ip -o -f inet addr show scope global 2>/dev/null | awk '{print $4}' | head -n1)"
  if [ -n "$NET" ]; then
    NET="$(python3 -c 'import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)' "$NET")"
    if ask_yes "Your firewall is on. Let devices on your home network ($NET) reach Rerun?"; then
      $SUDO ufw allow from "$NET" to any port "$PORT" proto tcp >/dev/null
    fi
  fi
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
URL="http://${IP:-localhost}:$PORT"
say "All set — Rerun is on the air."
info "Open it from your app menu (\"Rerun TV\"), or on any device at home: $URL"
info "Handy commands: rerun status · rerun update · rerun logs · rerun help"
[ "$INTEL_GPU" = 1 ] && info "Intel graphics found, so Rerun uses Quick Sync for video. To check it: rerun check-gpu"
if [ "$GUI" = 1 ]; then
  if zenity --question --title="$TITLE" --width=440 --ok-label="Open Rerun" --cancel-label="Close" \
    --text="Rerun is installed and running.

You'll find <b>Rerun TV</b> in your app menu. Other devices at home can open:
<b>$URL</b>" 2>/dev/null; then
    $SUDO -u "$ME" xdg-open "http://localhost:$PORT" >/dev/null 2>&1 || xdg-open "http://localhost:$PORT" >/dev/null 2>&1 || true
  fi
fi
