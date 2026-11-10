#!/usr/bin/env bash
# Install or update cambridge-ma-parking-checks on a Linux VM. Run as root. Idempotent.
#
# Kept fully separate from anything else on the host: its own system user (parkingbot), its own
# directory, venv, uv cache, state directory and parking-checks-* units. The only shared piece is
# the node's Tailscale daemon, where this adds one Funnel entry on port 8443 (port 443 untouched).
set -euo pipefail

APP=/opt/cambridge-ma-parking-checks
BOT=parkingbot
STATE=/var/lib/parking-checks
UV_CACHE=/var/cache/parking-checks-uv
FUNNEL_PORT=8443
INTAKE_PORT=8787
TIMERS=(parking-checks-tow-plate parking-checks-tow-freshness parking-checks-cleaning-nightly parking-checks-tow-sweep)

[ "$(id -u)" = 0 ] || { echo "install.sh must run as root" >&2; exit 1; }
[ -f "$APP/pyproject.toml" ] || { echo "expected the repo at $APP" >&2; exit 1; }
cd "$APP"

# --- user and directories ----------------------------------------------------------------------
if ! id "$BOT" >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE" --no-create-home --shell /usr/sbin/nologin "$BOT"
fi
install -d -o "$BOT" -g "$BOT" -m 700 "$STATE"
install -d -m 755 "$UV_CACHE"

# --- .env ---------------------------------------------------------------------------------------
if [ ! -f .env ]; then
  token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  sed "s|^INTAKE_TOKEN=.*|INTAKE_TOKEN=$token|" .env.example >.env
  echo "Created $APP/.env with a fresh INTAKE_TOKEN. Fill in the alert settings."
fi
# Shared secret for the Apps Script email relay (older .env files lack the key).
if ! grep -qE '^EMAIL_WEBHOOK_SECRET=.+' .env; then
  secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  if grep -q '^EMAIL_WEBHOOK_SECRET=' .env; then
    sed -i "s|^EMAIL_WEBHOOK_SECRET=.*|EMAIL_WEBHOOK_SECRET=$secret|" .env
  else
    printf '\n# Apps Script email relay (deploy/apps_script_email.gs)\nEMAIL_WEBHOOK_URL=\nEMAIL_WEBHOOK_SECRET=%s\n' \
      "$secret" >>.env
  fi
  echo "Generated EMAIL_WEBHOOK_SECRET in $APP/.env."
fi
chown "$BOT:$BOT" .env
chmod 600 .env

# --- code and venv (root-owned, read-only to the service) --------------------------------------
command -v uv >/dev/null || { echo "uv not found on PATH" >&2; exit 1; }
UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR="$UV_CACHE" UV_PROJECT_ENVIRONMENT="$APP/.venv" \
  uv sync --frozen --no-dev --python /usr/bin/python3 --quiet
find "$APP" -path "$APP/.env" -prune -o -exec chown root:root {} +
chmod -R go-w "$APP"
"$APP/.venv/bin/parking-checks" --help >/dev/null

# --- systemd units ------------------------------------------------------------------------------
fresh="$(sed -n 's/^FRESHNESS_ON_CALENDAR=//p' .env | tail -n1)"
fresh="${fresh:-Wed *-*-* 07:00:00 America/New_York}"
systemd-analyze calendar "$fresh" >/dev/null || { echo "bad FRESHNESS_ON_CALENDAR: $fresh" >&2; exit 1; }
for f in deploy/*.service deploy/*.timer; do
  sed "s|@FRESHNESS_ON_CALENDAR@|$fresh|" "$f" >"/etc/systemd/system/$(basename "$f")"
done
systemctl daemon-reload

# Config check as the service user (also creates the database).
if ! runuser -u "$BOT" -- env PARKING_CHECKS_ENV="$APP/.env" "$APP/.venv/bin/parking-checks" last-location >/dev/null; then
  echo "Config check failed; fix $APP/.env and re-run install.sh. Units are installed but not started." >&2
  exit 1
fi

systemctl enable --quiet parking-checks-intake.service
systemctl restart parking-checks-intake.service
for t in "${TIMERS[@]}"; do
  systemctl enable --quiet --now "$t.timer"
done

# --- Tailscale Funnel on :8443 -> intake ----------------------------------------------------------
if [ "${SKIP_FUNNEL:-0}" != 1 ]; then
  if ! tailscale funnel status 2>/dev/null | grep -q ":$FUNNEL_PORT"; then
    tailscale funnel --bg --https="$FUNNEL_PORT" "http://127.0.0.1:$INTAKE_PORT"
  fi
  tailscale funnel status || true
fi

# --- summary --------------------------------------------------------------------------------------
sleep 2
systemctl --no-pager --lines=0 status parking-checks-intake.service | head -n 3 || true
curl -fsS "http://127.0.0.1:$INTAKE_PORT/healthz" && echo
systemctl list-timers --no-pager 'parking-checks-*'
grep -qE '^(EMAIL_WEBHOOK_URL|SMTP_PASSWORD|TELEGRAM_CHAT_IDS)=.+' .env || \
  echo "WARNING: no alert channel configured yet in $APP/.env; alerts will only be logged."
