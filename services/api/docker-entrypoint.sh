#!/bin/sh
# A host's mounted disk arrives owned by root, so start as root just long enough to hand the
# storage folder to the app user, then drop privileges before anything else runs.
set -e
if [ "$(id -u)" = 0 ]; then
  root="${STORAGE_LOCAL_ROOT:-var/storage}"
  mkdir -p "$root"
  chown pressflow "$root"
  export HOME=/home/pressflow
  exec setpriv --reuid=10001 --regid=10001 --init-groups "$@"
fi
exec "$@"
