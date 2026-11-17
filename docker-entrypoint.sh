#!/bin/sh
# Fix the mounted /data ownership, then run the server as the unprivileged user.
set -e

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data
    chown app:app /data
    find /data -maxdepth 1 ! -user app -exec chown app:app {} +
    exec python -c 'import os, sys
os.setgid(10001)
os.setgroups([])
os.setuid(10001)
os.execvp(sys.argv[1], sys.argv[1:])' "$@"
fi

exec "$@"
