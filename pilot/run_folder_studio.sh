#!/bin/sh
# Session-scoped launcher. Waits for Immich and the local bridge without
# opening or focusing a browser window.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH

/usr/bin/python3 -u "$SCRIPT_DIR/folder_studio.py" &
bridge_pid=$!
trap 'kill "$bridge_pid" 2>/dev/null || true; wait "$bridge_pid" 2>/dev/null || true' EXIT INT TERM

attempt=0
while [ "$attempt" -lt 120 ]; do
  if ! kill -0 "$bridge_pid" 2>/dev/null; then
    wait "$bridge_pid"
    exit $?
  fi

  if /usr/bin/curl -fsS --max-time 2 http://127.0.0.1:2283/api/server/ping >/dev/null \
    && /usr/bin/nc -z -w 1 127.0.0.1 8766 >/dev/null 2>&1; then
    wait "$bridge_pid"
    exit $?
  fi

  /bin/sleep 1
  attempt=$((attempt + 1))
done

printf '%s\n' 'Immich is not reachable yet; the organize page was not opened.' >&2
wait "$bridge_pid"
exit $?
