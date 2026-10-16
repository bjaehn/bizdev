#!/bin/sh
set -e
mkdir -p "${BD_DATA_DIR:-/data}"
chown -R bd:bd "${BD_DATA_DIR:-/data}"
exec setpriv --reuid=10001 --regid="$(id -g bd)" --init-groups "$@"
