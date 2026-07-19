#!/bin/sh
set -eu

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
app_directory=$(CDPATH= cd -- "$script_directory/.." && pwd)
cd "$app_directory"
state_directory=$app_directory/.real-e2e
owner_marker=$state_directory/owner
proxy_pid=''
proxy_started=0
api_expected=1

owner_matches() {
  [ -d "$state_directory" ] &&
    [ ! -L "$state_directory" ] &&
    [ -f "$owner_marker" ] &&
    [ ! -L "$owner_marker" ] &&
    [ "$(wc -c < "$owner_marker" 2>/dev/null | tr -d ' ')" = 65 ] &&
    [ "$(cat "$owner_marker" 2>/dev/null)" = "$owner_token" ]
}

stop_proxy() {
  signal_name=$1
  if [ -z "$proxy_pid" ]; then
    return
  fi
  if ! kill -0 "$proxy_pid" 2>/dev/null; then
    wait "$proxy_pid" 2>/dev/null || true
    proxy_pid=''
    return
  fi
  kill -s "$signal_name" "$proxy_pid" 2>/dev/null || true
  (
    sleep 2
    kill -KILL "$proxy_pid" 2>/dev/null || true
  ) &
  kill_watchdog=$!
  wait "$proxy_pid" 2>/dev/null || true
  kill "$kill_watchdog" 2>/dev/null || true
  wait "$kill_watchdog" 2>/dev/null || true
  proxy_pid=''
}

stop_owned_api() {
  owner_matches || return 1
  api_pid_file=$state_directory/api.pid
  if [ ! -f "$api_pid_file" ]; then
    [ "$api_expected" -eq 0 ] || [ "$proxy_started" -eq 0 ]
    return
  fi
  api_pid=$(cat "$api_pid_file" 2>/dev/null || true)
  case "$api_pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  attempts=0
  while kill -0 "$api_pid" 2>/dev/null && [ "$attempts" -lt 15 ]; do
    sleep 0.1
    attempts=$((attempts + 1))
  done
  if kill -0 "$api_pid" 2>/dev/null; then
    kill -KILL "$api_pid" 2>/dev/null || true
  fi
  attempts=0
  while kill -0 "$api_pid" 2>/dev/null && [ "$attempts" -lt 5 ]; do
    sleep 0.1
    attempts=$((attempts + 1))
  done
  ! kill -0 "$api_pid" 2>/dev/null
}

remove_owned_state() {
  if owner_matches; then
    rm -rf "$state_directory"
  fi
}

cleanup() {
  status=$?
  trap - EXIT INT TERM HUP
  stop_proxy TERM
  if stop_owned_api; then
    remove_owned_state
  fi
  exit "$status"
}

forward_signal() {
  signal_name=$1
  exit_code=$2
  trap - "$signal_name"
  stop_proxy "$signal_name"
  exit "$exit_code"
}

umask 077
openssl_path=$(command -v openssl)
[ -n "$openssl_path" ] && [ -x "$openssl_path" ]
owner_token=$($openssl_path rand -hex 32)
export HRAMATKA_E2E_OWNER_TOKEN=$owner_token
export HRAMATKA_E2E_STATE_DIR=$state_directory
trap cleanup EXIT
trap 'forward_signal INT 130' INT
trap 'forward_signal TERM 143' TERM
trap 'forward_signal HUP 129' HUP
if ! mkdir -m 700 "$state_directory"; then
  echo "another real-backend E2E run owns .real-e2e" >&2
  exit 1
fi
printf '%s\n' "$owner_token" > "$owner_marker"

repo_python=$(cd ../.. && pwd)/.venv/bin/python
if [ "${CI:-}" = true ]; then
  e2e_python=$(command -v python || command -v python3)
  [ -n "$e2e_python" ] && [ -x "$e2e_python" ]
elif [ -x "$repo_python" ]; then
  e2e_python=$repo_python
else
  echo "repository Python is required outside CI" >&2
  exit 1
fi
ln -s "$e2e_python" "$state_directory/python"

npm run build
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj /CN=127.0.0.1 -addext subjectAltName=IP:127.0.0.1 \
  -keyout "$state_directory/key.pem" -out "$state_directory/cert.pem" \
  >/dev/null 2>&1
chmod 600 "$state_directory/key.pem" "$state_directory/cert.pem"

if [ "${HRAMATKA_E2E_TEST_HUNG_PROXY:-}" = 1 ]; then
  api_expected=0
  /bin/sh -c 'trap "" TERM INT HUP; while :; do sleep 1; done' &
else
  /usr/bin/env -u FORCE_COLOR -u NO_COLOR \
  PYTHONPATH=../.. \
  HRAMATKA_E2E_PYTHON="$state_directory/python" \
  HRAMATKA_E2E_ORIGIN=https://127.0.0.1:5174 \
  HRAMATKA_E2E_DB_PATH="$state_directory/pilot.sqlite3" \
  HRAMATKA_E2E_INVITE_PATH="$state_directory/invite-token" \
  HRAMATKA_PROXY_FRONTEND_DIR="$PWD/dist" \
  HRAMATKA_PROXY_TLS_CERT="$state_directory/cert.pem" \
  HRAMATKA_PROXY_TLS_KEY="$state_directory/key.pem" \
  HRAMATKA_PROXY_PORT=5174 \
  node e2e/https-static-proxy.mjs &
fi
proxy_pid=$!
proxy_started=1
printf '%s\n' "$proxy_pid" > "$state_directory/proxy.pid"
proxy_status=0
wait "$proxy_pid" || proxy_status=$?
proxy_pid=''
exit "$proxy_status"
