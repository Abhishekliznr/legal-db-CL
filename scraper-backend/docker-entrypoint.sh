#!/bin/sh
set -e

Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp -ac &

for i in $(seq 1 50); do
    [ -e /tmp/.X11-unix/X99 ] && break
    sleep 0.2
done

export DISPLAY=:99

exec "$@"
