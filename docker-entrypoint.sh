#!/bin/sh
# Runs as root only long enough to make the shared download volume writable
# for the unprivileged "app" user, then execs the real command as "app".
set -e

if [ "$(id -u)" = "0" ]; then
    mkdir -p /downloads/tmp
    chown app:app /downloads /downloads/tmp 2>/dev/null || true
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi

exec "$@"
