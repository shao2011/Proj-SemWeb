#!/bin/sh
# Starts as root only to make the Firebase credential readable, then runs the
# command as the unprivileged app user. Render mounts Secret Files under
# /etc/secrets owned by root, which uid 10001 cannot open, and the API refuses
# to start without that credential.
set -eu

APP_UID=10001
APP_GID=10001

if [ "$(id -u)" != 0 ]; then
    exec "$@"
fi

drop() {
    setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups "$@"
}

credential="${FIREBASE_SERVICE_ACCOUNT_PATH:-}"
if [ -n "$credential" ] && [ -f "$credential" ] && ! drop test -r "$credential"; then
    copy=/app/run/firebase-service-account.json
    install -o "$APP_UID" -g "$APP_GID" -m 0400 "$credential" "$copy"
    export FIREBASE_SERVICE_ACCOUNT_PATH="$copy"
fi

# A volume written while the image still ran as root holds root-owned
# directories and files; only those are touched, so a large volume that is
# already right costs one walk and no writes. This runs as root over a tree
# the app user can write to, so symlinks are never followed (-h) and files
# with other hard links are skipped: either could point the chown at a
# system file.
find /app/data \( -type d -o -links 1 \) ! -user "$APP_UID" \
    -exec chown -h "$APP_UID:$APP_GID" {} + \
    || echo "entrypoint: could not chown everything under /app/data" >&2

export HOME=/home/app
exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups "$@"
