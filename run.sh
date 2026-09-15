#!/bin/bash
# The scoreboard, exactly once.
#
#   ./run.sh                 make sure one board is running (start it if not), then print its URLs
#   ./run.sh --simulate      the same, with extra options passed to the board (see python -m swaptx --help)
#   ./run.sh status          is it up? which process, which port, since when
#   ./run.sh restart [...]   stop it and start it again
#   ./run.sh stop            stop it
#   ./run.sh fg [...]        run it in the foreground instead (Ctrl-C stops it)
#   ./run.sh log             follow the log
#
# Safe to run any time: a second copy is never started, a stray duplicate is stopped, and a
# board that is up but not answering is restarted. One board per port; --http-port N picks it.
set -u
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  PY=$(command -v python3.12 || command -v python3)
  "$PY" -m venv .venv && ./.venv/bin/pip install -q -r requirements.txt || exit 1
fi

CMD=run
case "${1:-}" in
  status|stop|restart|fg|log) CMD=$1; shift ;;
esac

PORT=8000
prev=""
for a in "$@"; do
  case "$prev" in --http-port) PORT=$a ;; esac
  case "$a" in --http-port=*) PORT=${a#--http-port=} ;; esac
  prev=$a
done
mkdir -p data
LOG=data/server-$PORT.log
URL=http://127.0.0.1:$PORT

# Every process running this board on this port: the launcher's own python (args "-m swaptx"),
# with either no --http-port or ours.
board_pids() {
  pgrep -f -- "-m swaptx" 2>/dev/null | while read -r p; do
    args=$(ps -o args= -p "$p" 2>/dev/null) || continue
    case "$args" in
      *--http-port[\ =]"$PORT"*) echo "$p" ;;
      *--http-port*) ;;
      *) [ "$PORT" = 8000 ] && echo "$p" ;;
    esac
  done
}
listener() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1; }
healthy() { curl -s -m 2 "$URL/api/health" 2>/dev/null | grep -q '"ok":true'; }
lan_ip() { ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname; }

stop_all() {
  pids=$(board_pids)
  [ -z "$pids" ] && return 0
  echo "stopping: $(echo "$pids" | tr '\n' ' ')"
  kill $pids 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.5
    [ -z "$(board_pids)" ] && return 0
  done
  kill -9 $(board_pids) 2>/dev/null
  sleep 0.5
}

start() {
  nohup ./.venv/bin/python -m swaptx "$@" >> "$LOG" 2>&1 &
  for _ in $(seq 1 60); do
    sleep 0.5
    healthy && return 0
  done
  echo "the board did not come up; last lines of $LOG:" >&2
  tail -20 "$LOG" >&2
  return 1
}

show() {
  pid=$(listener)
  started=$(ps -o lstart= -p "$pid" 2>/dev/null | tr -s " " | sed "s/^ //;s/ $//")
  dongle=$(curl -s -m 2 "$URL/api/health" 2>/dev/null | ./.venv/bin/python -c 'import json,sys
try:
    d = json.load(sys.stdin)["dongle"]
    print(("dongle on " + str(d.get("port"))) if d.get("connected") else ("no dongle" + ((": " + d["last_error"]) if d.get("last_error") else "")))
except Exception:
    print("?")' 2>/dev/null)
  echo "running: pid $pid since $started · $dongle"
  echo "  wall   http://$(lan_ip):$PORT/"
  echo "  admin  http://$(lan_ip):$PORT/admin"
  echo "  log    $LOG"
}

case "$CMD" in
  status)
    if healthy; then show; else
      pids=$(board_pids)
      if [ -n "$pids" ]; then echo "not answering on port $PORT, but these are running: $(echo "$pids" | tr '\n' ' ')"; else echo "not running"; fi
      exit 1
    fi ;;
  stop) stop_all; echo "stopped" ;;
  restart) stop_all; start "$@" && show ;;
  fg) stop_all; exec ./.venv/bin/python -m swaptx "$@" ;;
  log) tail -f "$LOG" ;;
  run)
    keep=$(listener)
    # a duplicate that is not the one holding the port is a stray: stop it
    for p in $(board_pids); do
      [ "$p" = "$keep" ] && continue
      echo "stopping stray copy $p"
      kill "$p" 2>/dev/null
    done
    if [ -n "$keep" ] && healthy; then
      echo "already up"
      show
    else
      [ -n "$keep" ] && { echo "up but not answering: restarting"; kill "$keep" 2>/dev/null; sleep 1; }
      stop_all
      start "$@" && show
    fi ;;
esac
