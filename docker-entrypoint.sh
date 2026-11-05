#!/bin/sh
# Runs as root only long enough to make the shared download volume writable
# for the unprivileged "app" user, then execs the real command as "app".
set -e

if [ "$(id -u)" = "0" ]; then
    mkdir -p /downloads/tmp
    chown app:app /downloads /downloads/tmp 2>/dev/null || true
    # setpriv keeps root's environment: without HOME=/home/app, libraries
    # that look in ~ (asyncpg reads ~/.postgresql/postgresql.key for the
    # optional client cert) hit /root and fail with "Permission denied".
    export HOME=/home/app USER=app LOGNAME=app
    # Keep the supplementary groups the container runtime gave us (compose
    # `group_add`, e.g. the Local Bot API's gid 101 so recognition can read
    # its 0640 voice/video files). `--init-groups` would replace them with
    # /etc/group's view of "app", which knows nothing about group_add.
    extra=""
    for g in $(id -G); do
        if [ "$g" != 0 ]; then extra="$extra,$g"; fi
    done
    if [ -n "$extra" ]; then
        exec setpriv --reuid=app --regid=app --groups="app$extra" "$@"
    fi
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi

exec "$@"
