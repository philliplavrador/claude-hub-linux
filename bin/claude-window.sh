#!/bin/sh
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH"; export PATH ;; esac
if [ "${1:-}" = "--own-window" ]; then
  DIR=${CLAUDE_HUB_WINDOW_DIR:-$HOME}
  TITLE="claude · $(date +%H:%M)"
  exec konsole --workdir "$DIR" -p "tabtitle=$TITLE" -e bash -lc '
    ACCT=$(claude-autoswap recommend 2>/dev/null)
    ACCTS=${CLAUDE_HUB_ACCOUNTS:-$HOME/.claude-accounts}
    if [ -z "$ACCT" ] || [ ! -f "$ACCTS/$ACCT/.credentials.json" ]; then
      PICK=$(mktemp -t claude-account-XXXXXX)
      claude-account-menu --out "$PICK" --title "Open Claude as which account?" || {
        rm -f "$PICK"; exit 0; }
      ACCT=$(cat "$PICK"); rm -f "$PICK"
    fi
    cd '"$DIR"' || exit 1
    exec claude-window new "$ACCT"'
fi

hub_fast() {
  if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    d=${DISPLAY:-}; d=${d%%.*}
    [ "$d" = "${CLAUDE_HUB_HIDDEN_DISPLAY:-:20}" ] && return 1
  fi
  f=${CLAUDE_HUB_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/claude-hub/claude-hub.json}
  [ -r "$f" ] || return 1
  pid=$(jq -r 'if .kind == "popup" then (.pid | tostring) else empty end' "$f" 2>/dev/null) || return 1
  case $pid in ''|*[!0-9]*) return 1 ;; esac
  [ "${#pid}" -le 10 ] && [ "$pid" -gt 0 ] && [ "$pid" -le 2147483647 ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  grep -aq claude-hub "/proc/$pid/cmdline" 2>/dev/null || return 1
  kill -USR1 "$pid" 2>/dev/null || return 1
  return 0
}
if hub_fast; then exit 0; fi
exec claude-hub show
