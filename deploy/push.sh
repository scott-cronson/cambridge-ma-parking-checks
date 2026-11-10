#!/usr/bin/env bash
# Deploy the committed HEAD to the server over SSH and run install.sh.
# Usage: deploy/push.sh root@your-vm     (or set DEPLOY_HOST)
# No credentials are stored on the server: the tree is streamed with git archive.
set -euo pipefail

HOST="${1:-${DEPLOY_HOST:-}}"
[ -n "$HOST" ] || { echo "usage: deploy/push.sh user@host (or set DEPLOY_HOST)" >&2; exit 2; }
APP=/opt/cambridge-ma-parking-checks
cd "$(git rev-parse --show-toplevel)"
git diff --quiet HEAD -- || { echo "Uncommitted changes; commit first." >&2; exit 1; }

echo "Deploying $(git rev-parse --short HEAD) to $HOST:$APP"
git archive --format=tar HEAD | ssh "$HOST" "
  set -e
  rm -rf $APP.new && mkdir -p $APP.new $APP && tar -x -C $APP.new
  rsync -a --delete --exclude /.env --exclude /.venv $APP.new/ $APP/
  rm -rf $APP.new
  echo $(git rev-parse HEAD) > $APP/REVISION
"
ssh "$HOST" "bash $APP/deploy/install.sh"
