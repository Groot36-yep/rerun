#!/usr/bin/env bash
# The "rerun" command, installed to /usr/local/bin/rerun. Run "rerun help" to see what it does.
set -euo pipefail

APP_DIR=/opt/rerun
DATA_DIR=/var/lib/rerun
PORT=8410
RUN_USER=""
REPO=Groot36-yep/rerun
# shellcheck disable=SC1091
[ -r /etc/rerun.conf ] && . /etc/rerun.conf
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

as_user() { if [ "$(id -un)" = "$1" ]; then shift; "$@"; else local u="$1"; shift; sudo -u "$u" "$@"; fi; }

healthy() {
  for _ in $(seq 1 30); do
    "$APP_DIR/venv/bin/python" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/health', timeout=3)" 2>/dev/null && return 0
    sleep 2
  done
  return 1
}

backup() {
  local dir="$HOME/rerun-backups" f
  mkdir -p "$dir"
  f="$dir/rerun-settings-$(date +%Y%m%d-%H%M%S).tar.gz"
  $SUDO tar -czf "$f" -C "$DATA_DIR" config.json secrets.json
  $SUDO chown "$(id -u):$(id -g)" "$f"
  chmod 600 "$f"
  echo "$f"
}

case "${1:-help}" in
  status)
    if systemctl is-active --quiet rerun; then
      echo "Rerun is running. Open http://$(hostname -I | awk '{print $1}'):$PORT"
    else
      echo "Rerun is stopped. Start it with: rerun start"
    fi
    ;;
  open)
    xdg-open "http://localhost:$PORT" >/dev/null 2>&1 &
    ;;
  logs)
    journalctl -u rerun -n "${2:-150}" --no-pager
    ;;
  follow)
    journalctl -u rerun -f -n 50
    ;;
  restart)
    $SUDO systemctl restart rerun
    healthy && echo "Restarted." || echo "Not answering yet; check: rerun logs"
    ;;
  stop)
    $SUDO systemctl disable --now rerun
    echo "Stopped. It stays off (even after a restart) until you run: rerun start"
    ;;
  start)
    $SUDO systemctl enable --now rerun
    healthy && echo "Running." || echo "Not answering yet; check: rerun logs"
    ;;
  backup)
    echo "Saved $(backup)"
    ;;
  restore)
    [ -f "${2:-}" ] || { echo "Usage: rerun restore ~/rerun-backups/rerun-settings-XXXX.tar.gz"; exit 1; }
    $SUDO systemctl stop rerun
    $SUDO tar -xzf "$2" -C "$DATA_DIR"
    $SUDO chown -R "$RUN_USER" "$DATA_DIR"
    $SUDO systemctl start rerun
    healthy && echo "Restored." || echo "Not answering yet; check: rerun logs"
    ;;
  update)
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    echo "Downloading the latest Rerun from github.com/$REPO …"
    python3 -c 'import sys, urllib.request; sys.stdout.buffer.write(urllib.request.urlopen(sys.argv[1], timeout=60).read())' \
      "https://github.com/$REPO/archive/refs/heads/main.tar.gz" | tar -xz -C "$tmp" \
      || { echo "Couldn't download it. Check the internet connection and try again."; exit 1; }
    bash "$tmp"/*/install.sh
    ;;
  check-gpu)
    dev="$(ls /dev/dri/renderD* 2>/dev/null | head -n1 || true)"
    for d in /dev/dri/renderD*; do
      [ -e "$d" ] && [ "$(cat "/sys/class/drm/$(basename "$d")/device/vendor" 2>/dev/null)" = 0x8086 ] && { dev="$d"; break; }
    done
    [ -n "$dev" ] || { echo "No graphics chip found (/dev/dri is empty), so Rerun uses the processor."; exit 0; }
    echo "Graphics chip: $dev ($(cat "/sys/class/drm/$(basename "$dev")/device/vendor" 2>/dev/null | sed 's/0x8086/Intel/;s/0x1002/AMD/;s/0x10de/NVIDIA/'))"
    user="${RUN_USER:-$USER}"
    id -nG "$user" | grep -qw render || echo "Warning: $user isn't in the 'render' group. Fix: sudo usermod -aG render,video $user, then reboot."
    command -v vainfo >/dev/null && { echo; as_user "$user" vainfo --display drm --device "$dev" 2>&1 | grep -E "Driver version|VAProfileH264.*EncSlice" || echo "vainfo found no H.264 encoder."; }
    echo; echo "Test encode with Quick Sync / VA-API:"
    for lp in 0 1; do
      if as_user "$user" ffmpeg -hide_banner -loglevel error -init_hw_device "vaapi=va:$dev" -filter_hw_device va \
          -f lavfi -i testsrc2=s=1280x720:r=30 -t 3 -vf format=nv12,hwupload -c:v h264_vaapi -low_power "$lp" -f null - 2>/tmp/rerun-gpu.$$; then
        [ "$lp" = 1 ] && echo "  Works (low-power mode)." || echo "  Works."
        rm -f /tmp/rerun-gpu.$$; exit 0
      fi
    done
    echo "  Didn't work:"; sed 's/^/    /' /tmp/rerun-gpu.$$ | tail -n 5; rm -f /tmp/rerun-gpu.$$
    echo "  On Intel, installing the full driver usually fixes it: sudo apt install intel-media-va-driver-non-free"
    ;;
  reset-password)
    $SUDO -u "${RUN_USER:-$USER}" env RERUN_DATA="$DATA_DIR" PYTHONPATH="$APP_DIR" "$APP_DIR/venv/bin/python" -m app reset-password
    $SUDO systemctl restart rerun
    ;;
  uninstall)
    echo "This removes Rerun but keeps your channels and settings in $DATA_DIR."
    read -r -p "Continue? (y/n) " a
    [[ "$a" =~ ^[Yy] ]] || exit 0
    $SUDO systemctl disable --now rerun 2>/dev/null || true
    $SUDO rm -f /etc/systemd/system/rerun.service
    $SUDO systemctl daemon-reload
    $SUDO rm -rf "$APP_DIR"
    rm -f "$HOME/.local/share/applications/rerun.desktop"
    $SUDO rm -f /usr/local/bin/rerun
    echo "Rerun is removed. To delete your settings too: sudo rm -rf $DATA_DIR /etc/rerun.conf"
    ;;
  *)
    cat <<'EOF'
rerun — commands for your Rerun TV channels

  rerun status          Is it running? Shows its web address.
  rerun open            Open Rerun in your web browser.
  rerun logs            Show recent activity (add a number for more lines).
  rerun follow          Watch activity live (Ctrl+C to stop watching).
  rerun restart         Restart Rerun.
  rerun stop / start    Turn it off (stays off) or back on.
  rerun backup          Save your channels and settings to ~/rerun-backups.
  rerun restore FILE    Put back a backup.
  rerun update          Download the newest Rerun and install it (keeps your settings).
  rerun check-gpu       Check that Intel Quick Sync (or AMD) video encoding works.
  rerun reset-password  Forgot the web password? Clears it so you can choose a new one.
  rerun uninstall       Remove Rerun (keeps your settings).

"rerun update" also lets you change your media folders or the port.
EOF
    ;;
esac
