#!/usr/bin/env bash
# install.sh: set up claude-hub-linux on Kubuntu/Ubuntu 26.04 or later (KDE Plasma 6, Wayland).
#
#   ./install.sh [--parts hub,autoswap,desktops,toolkit] [--accounts a,b,...] [--yes] [--dry-run] [--no-shortcuts]
#
#   --parts     what to install, comma separated; default all
#                 hub       the Claude hub popup (claude-hub)
#                 autoswap  auto-switching between Claude subscriptions (claude-autoswap and friends)
#                 desktops  one KDE desktop per Claude, the guards that keep agents off your screen
#                 toolkit   the hooks and skills, installed into every Claude account folder
#   --accounts  names of your Claude subscriptions (folders under ~/.claude-accounts); asked for when omitted
#   --yes       no questions: install packages without asking, and make only the accounts named by --accounts
#   --dry-run   print what would be done and change nothing
#   --no-shortcuts  do not bind Meta+Return to the hub popup (it is bound only when no other action holds that key)
#
# Safe to run again: a rerun updates links, units, KWin packages and the toolkit copy, and changes nothing else.
# Environment: INSTALL_ROOT (the clone to install from; default: this script's folder), HOME,
# CLAUDE_HUB_OS_RELEASE (an os-release file to read instead of /etc/os-release; for tests).
set -euo pipefail

ROOT=${INSTALL_ROOT:-$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)}
OS_RELEASE=${CLAUDE_HUB_OS_RELEASE:-/etc/os-release}
DATA="$HOME/.local/share/claude-hub"
STORE="$DATA/toolkit"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
ACCT_DIR="$HOME/.claude-accounts"

PARTS=hub,autoswap,desktops,toolkit
YES=0
DRY=0
ACCOUNTS_ARG=
HAVE_ACCOUNTS_ARG=0
SHORTCUTS=1

die() { echo "install: $*" >&2; exit 1; }
say() { echo "$*"; }
step() { echo; echo "== $*"; }

usage() { sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while (( $# )); do
  case $1 in
    --parts) [[ $# -ge 2 ]] || die "--parts needs a value"; PARTS=$2; shift 2;;
    --parts=*) PARTS=${1#--parts=}; shift;;
    --accounts) [[ $# -ge 2 ]] || die "--accounts needs a value"; ACCOUNTS_ARG=$2; HAVE_ACCOUNTS_ARG=1; shift 2;;
    --accounts=*) ACCOUNTS_ARG=${1#--accounts=}; HAVE_ACCOUNTS_ARG=1; shift;;
    --yes|-y) YES=1; shift;;
    --dry-run) DRY=1; shift;;
    --no-shortcuts) SHORTCUTS=0; shift;;
    -h|--help) usage; exit 0;;
    *) usage >&2; die "unknown option: $1";;
  esac
done

# ---- parts -------------------------------------------------------------------------------------------------
HUB=0 AUTOSWAP=0 DESKTOPS=0 TOOLKIT=0
IFS=, read -r -a _parts <<<"$PARTS"
for p in "${_parts[@]}"; do
  case $p in
    all) HUB=1 AUTOSWAP=1 DESKTOPS=1 TOOLKIT=1;;
    hub) HUB=1;;
    autoswap) AUTOSWAP=1;;
    desktops) DESKTOPS=1;;
    toolkit) TOOLKIT=1;;
    *) die "unknown part '$p' (choose from hub, autoswap, desktops, toolkit)";;
  esac
done
(( HUB + AUTOSWAP + DESKTOPS + TOOLKIT > 0 )) || die "--parts names nothing to install"
APPS=$(( HUB + AUTOSWAP + DESKTOPS ))     # parts that bring commands, units and packages

valid_name() { [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; }
if (( HAVE_ACCOUNTS_ARG )); then          # refused before anything is changed
  IFS=, read -r -a _given <<<"$ACCOUNTS_ARG"
  for _nm in "${_given[@]}"; do
    valid_name "$_nm" || die "'$_nm' is not a usable account name (start with a letter or digit; then letters, digits, - _ .)"
  done
fi

# ---- helpers -----------------------------------------------------------------------------------------------
# Run a command, or only print it under --dry-run.
doit() {
  if (( DRY )); then echo "  + $*"; else "$@"; fi
}

as_root() {
  if [[ $(id -u) == 0 ]]; then doit "$@"
  else command -v sudo >/dev/null 2>&1 || die "sudo is needed to install packages and it is not installed"
    doit sudo "$@"
  fi
}

ver_ge() { [[ $(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1) == "$2" ]]; }   # ver_ge A B: A >= B

# Write stdin to $1 only when the content differs; keeps a rerun from touching anything.
write_if_changed() {
  local dest=$1 tmp
  tmp=$(mktemp "$dest.XXXXXX")
  cat >"$tmp"
  if [[ -f $dest ]] && cmp -s "$tmp" "$dest"; then rm -f "$tmp"; return 1; fi
  mv "$tmp" "$dest"
}

link_one() {   # link_one SRC DEST
  local src=$1 dest=$2 did=linked redid=relinked
  (( DRY )) && { did="would link"; redid="would relink"; }
  if [[ -L $dest ]]; then
    [[ $(readlink "$dest") == "$src" ]] && return 0
    doit ln -sfn "$src" "$dest"; say "  $redid $(basename "$dest")"; return 0
  fi
  if [[ -e $dest ]]; then
    say "  skipped $(basename "$dest"): $dest exists and is not a link (left alone)"; return 0
  fi
  doit ln -s "$src" "$dest"; say "  $did $(basename "$dest")"
}

# ---- 1. the machine ----------------------------------------------------------------------------------------
step "Checking this machine"
(( DRY )) || [[ -w $HOME ]] || die "$HOME is not writable"
if [[ $(id -u) == 0 && -z ${CLAUDE_HUB_ALLOW_ROOT:-} ]]; then
  die "run this as your normal user, not root (it installs into your home folder and uses sudo only for packages)"
fi
[[ -d $ROOT/bin ]] || die "$ROOT has no bin/ folder: run install.sh from a clone of the repository"

[[ -r $OS_RELEASE ]] || die "cannot read $OS_RELEASE: this installer is for Kubuntu or Ubuntu 26.04 or later"
os_id=$(. "$OS_RELEASE"; echo "${ID:-}")
os_ver=$(. "$OS_RELEASE"; echo "${VERSION_ID:-}")
case $os_id in
  ubuntu|kubuntu) ;;
  *) die "this is '${os_id:-unknown}', and the installer supports Kubuntu (or Ubuntu with KDE Plasma) 26.04 or later only";;
esac
[[ -n $os_ver ]] && ver_ge "$os_ver" 26.04 \
  || die "Ubuntu $os_ver is too old: Kubuntu 26.04 or later is required (Plasma 6)"

command -v plasmashell >/dev/null 2>&1 || die "KDE Plasma was not found (plasmashell is missing): install Kubuntu / the kubuntu-desktop package"
plasma_out=$(plasmashell --version 2>/dev/null || true)
if [[ $plasma_out =~ ([0-9]+)\.([0-9]+) ]]; then plasma_major=${BASH_REMATCH[1]}; else plasma_major=0; fi
(( plasma_major >= 6 )) || die "Plasma 6 is required, found '${plasma_out:-unknown}'"
[[ ${XDG_SESSION_TYPE:-} == wayland ]] \
  || die "a Wayland Plasma session is required (XDG_SESSION_TYPE is '${XDG_SESSION_TYPE:-unset}'): log in to 'Plasma (Wayland)' and run this from a terminal there"
say "  Ubuntu $os_ver, Plasma $plasma_out, Wayland: ok"

# ---- 2. packages -------------------------------------------------------------------------------------------
step "Packages"
pkgs=(python3 python3-dbus python3-gi jq nodejs)
(( HUB )) && pkgs+=(konsole python3-pyqt6 wl-clipboard qdbus-qt6 tmux)
(( AUTOSWAP )) && pkgs+=(konsole qdbus-qt6 sqlite3)
(( DESKTOPS )) && pkgs+=(konsole qdbus-qt6 wmctrl xdotool tmux kpackagetool6 libkf6config-bin xvfb)
(( HUB && SHORTCUTS )) && pkgs+=(libkf6config-bin)   # kwriteconfig6 binds the key
(( TOOLKIT )) && pkgs+=(rsync)
mapfile -t pkgs < <(printf '%s\n' "${pkgs[@]}" | sort -u)
missing=()
for p in "${pkgs[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if (( ${#missing[@]} )); then
  say "  to install: ${missing[*]}"
  ok=1
  if (( ! YES && ! DRY )); then
    read -r -p "  Install these with sudo apt-get? [Y/n] " ans || ans=
    [[ -z $ans || $ans =~ ^[Yy] ]] || ok=0
  fi
  if (( ok )); then
    as_root apt-get install -y --no-install-recommends "${missing[@]}" \
      || { say "  apt-get failed; refreshing the package lists and trying once more"
           as_root apt-get update
           as_root apt-get install -y --no-install-recommends "${missing[@]}"; }
  else
    die "packages declined: nothing installed"
  fi
else
  say "  every package is already installed"
fi

# Node: the hooks, the toolkit and the KWin script check need 22 or later.
node_ver=
command -v node >/dev/null 2>&1 && node_ver=$(node --version 2>/dev/null | sed 's/^v//') || true
if [[ -z $node_ver ]] || ! ver_ge "$node_ver" 22; then
  if (( DRY )); then
    say "  note: node ${node_ver:-missing}; 22 or later is needed (a real run stops here)"
  else
    cat >&2 <<EOF
install: Node.js 22 or later is required, found '${node_ver:-none}'.
  Install it, then run this script again. Either:
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y nodejs
  or install nvm (https://github.com/nvm-sh/nvm) and run: nvm install 22
EOF
    exit 1
  fi
else
  say "  node $node_ver: ok"
fi

if command -v claude >/dev/null 2>&1; then
  say "  claude: found at $(command -v claude)"
else
  say "  note: Claude Code ('claude') is not on your PATH. Install it first, following the official instructions:"
  say "        https://docs.claude.com/en/docs/claude-code/setup"
  say "        (the rest of the install carries on; sign-in needs it)"
fi

# ---- 3. commands -------------------------------------------------------------------------------------------
# Which part a command or unit belongs to; anything not named is shared and goes in with any part.
part_of() {
  case $1 in
    claude-hub) echo hub;;
    claude-autoswap|claude-autoswap.*|claude-move|claude-account-hook) echo autoswap;;
    claude-desks|claude-desktops.*|claude-agent-screen|claude-agent-screen.*|claude-agents-*|\
claude-console-*|claude-hidden-display.*|agents.slice) echo desktops;;
    *) echo core;;
  esac
}
wanted() {   # wanted PART
  case $1 in
    hub) (( HUB ));; autoswap) (( AUTOSWAP ));; desktops) (( DESKTOPS ));;
    core) (( APPS ));; *) return 1;;
  esac
}

if (( APPS )); then
  step "Commands into $BIN_DIR"
  doit mkdir -p "$BIN_DIR"
  for f in "$ROOT"/bin/*; do
    [[ -f $f || -L $f ]] || continue
    n=$(basename "$f")
    wanted "$(part_of "$n")" && link_one "$f" "$BIN_DIR/$n"
  done
  # agent-bin/ holds shims that stand in front of other programs (qdbus6, firefox, ...) on an agent's PATH. They are
  # used in place by the agent-screen service and must never go on your own PATH. Only the claude-* tools in it
  # are linked.
  if (( DESKTOPS )) && [[ -d $ROOT/agent-bin ]]; then
    for f in "$ROOT"/agent-bin/claude-*; do
      [[ -e $f || -L $f ]] || continue
      n=$(basename "$f")
      [[ -e $ROOT/bin/$n || -L $ROOT/bin/$n ]] && continue     # bin/ has the real one
      link_one "$f" "$BIN_DIR/$n"
    done
  fi
  case ":$PATH:" in *":$BIN_DIR:"*) ;; *) say "  note: $BIN_DIR is not on your PATH; add it to your shell profile";; esac
fi

# ---- 4. systemd user units ---------------------------------------------------------------------------------
if (( APPS )) && [[ -d $ROOT/systemd ]]; then
  step "systemd user units into $UNIT_DIR"
  doit mkdir -p "$UNIT_DIR"
  to_enable=()
  for f in "$ROOT"/systemd/*; do
    [[ -f $f ]] || continue
    n=$(basename "$f")
    wanted "$(part_of "$n")" || continue
    if [[ ! -f $UNIT_DIR/$n ]] || ! cmp -s "$f" "$UNIT_DIR/$n"; then
      doit cp "$f" "$UNIT_DIR/$n"; say "  installed $n"
    fi
    # Enable what asks to be enabled: it has an [Install] section, and is not a template ("name@.service").
    if [[ $n != *@* && $n =~ \.(service|timer|path|socket)$ ]] && grep -q '^\[Install\]' "$f"; then
      to_enable+=("$n")
    fi
  done
  # The user manager's PATH comes from the login that started it. Right after a first install it has no ~/.local/bin
  # (where Claude Code puts `claude`), so a unit that runs `claude` would not find it until the next login: add it now.
  if (( ! DRY )); then
    mgr_path=$(systemctl --user show-environment 2>/dev/null | sed -n 's/^PATH=//p' | head -n 1 || true)
    case ":$mgr_path:" in
      *":$BIN_DIR:"*) ;;
      *) if systemctl --user set-environment "PATH=$BIN_DIR:${mgr_path:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"; then
           say "  added $BIN_DIR to the background services' PATH for this session (it is there by itself after your next login)"
         else
           say "  warning: could not add $BIN_DIR to the background services' PATH; log out and in once"
         fi;;
    esac
  fi
  doit systemctl --user daemon-reload
  for u in "${to_enable[@]}"; do
    doit systemctl --user enable --now "$u" || say "  warning: could not enable $u (see: systemctl --user status $u)"
  done
  (( ${#to_enable[@]} )) && say "  enabled: ${to_enable[*]}"
fi

# ---- 5. KWin script packages -------------------------------------------------------------------------------
if (( DESKTOPS )) && [[ -d $ROOT/kwin ]]; then
  step "KWin scripts"
  kwin_ids=()
  hold_seen=0
  for d in "$ROOT"/kwin/*/; do
    [[ -f $d/metadata.json ]] || continue
    id=$(jq -r '.KPlugin.Id // empty' "$d/metadata.json")
    [[ -n $id ]] || { say "  skipped $(basename "$d"): metadata.json has no KPlugin.Id"; continue; }
    if [[ -d $HOME/.local/share/kwin/scripts/$id ]]; then
      doit kpackagetool6 --type=KWin/Script -u "${d%/}"
    else
      doit kpackagetool6 --type=KWin/Script -i "${d%/}"
    fi
    doit kwriteconfig6 --file kwinrc --group Plugins --key "${id}Enabled" true
    kwin_ids+=("$id")
    [[ $(basename "${d%/}") == desktop-hold ]] && hold_seen=1
    say "  $id: installed and enabled"
  done
  if (( ${#kwin_ids[@]} )); then
    # The hold script (desktop-hold) is half of a pair: KWin must also stop following a focused window to its desktop.
    if (( hold_seen )); then
      doit kwriteconfig6 --file kwinrc --group Windows --key ActivationDesktopPolicy DoNothing
    fi
    doit qdbus6 org.kde.KWin /KWin reconfigure
    if [[ -x $ROOT/kwin/reload.sh ]]; then
      for id in "${kwin_ids[@]}"; do
        loaded=$(qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.isScriptLoaded "$id" 2>/dev/null || true)
        if [[ $loaded == true ]]; then doit "$ROOT/kwin/reload.sh" "$id" || say "  warning: could not reload $id; it loads at your next login"
        else doit "$ROOT/kwin/reload.sh" "$id" --start || say "  warning: could not start $id; it loads at your next login"
        fi
      done
    else
      say "  the scripts load at your next login"
    fi
  fi
fi

# ---- 6. Claude subscriptions -------------------------------------------------------------------------------
step "Claude subscriptions"
existing=()
if [[ -d $ACCT_DIR ]]; then
  for d in "$ACCT_DIR"/*/; do [[ -d $d ]] && existing+=("$(basename "$d")"); done
fi
names=()
if (( HAVE_ACCOUNTS_ARG )); then
  IFS=, read -r -a names <<<"$ACCOUNTS_ARG"
elif (( YES )); then
  names=()
elif (( DRY )); then
  names=()
else
  (( ${#existing[@]} )) && say "  already set up: ${existing[*]}"
  read -r -p "  How many Claude subscriptions will you use? [${#existing[@]}] " count || count=
  [[ -n $count ]] || count=${#existing[@]}
  [[ $count =~ ^[0-9]+$ ]] || die "'$count' is not a number"
  for ((i = 1; i <= count; i++)); do
    default=${existing[i-1]:-}
    read -r -p "  Name for subscription $i (letters, digits, - _ .)${default:+ [$default]}: " nm || nm=
    nm=${nm:-$default}
    [[ -n $nm ]] || die "subscription $i has no name"
    names+=("$nm")
  done
fi
for nm in "${names[@]}"; do
  valid_name "$nm" || die "'$nm' is not a usable account name (start with a letter or digit; then letters, digits, - _ .)"
done
for nm in "${names[@]}"; do
  d=$ACCT_DIR/$nm
  if [[ ! -d $d ]]; then doit mkdir -p "$d"; say "  made $d"; fi
  if [[ ! -e $d/settings.json ]]; then
    if (( DRY )); then echo "  + write $d/settings.json"; else echo '{}' >"$d/settings.json"; fi
  fi
done
ALL_ACCOUNTS=()
if [[ -d $ACCT_DIR ]]; then
  for d in "$ACCT_DIR"/*/; do [[ -d $d ]] && ALL_ACCOUNTS+=("${d%/}"); done
fi
(( ${#ALL_ACCOUNTS[@]} )) || say "  none yet: rerun with --accounts name1,name2 when you are ready"

# Register one hook in one account's settings.json: add_hook SETTINGS EVENT MATCHER COMMAND (idempotent).
add_hook() {
  local file=$1 event=$2 matcher=$3 cmd=$4
  [[ -f $file ]] || return 0
  if (( DRY )); then echo "  + hook $event: $cmd in $file"; return 0; fi
  jq -e . "$file" >/dev/null 2>&1 || { say "  warning: $file is not valid JSON; left alone"; return 0; }
  local out
  out=$(jq --arg ev "$event" --arg m "$matcher" --arg cmd "$cmd" '
      .hooks = (.hooks // {})
      | .hooks[$ev] = ((.hooks[$ev] // []) as $e
          | if any($e[]; any(.hooks[]?; .command == $cmd)) then $e
            else $e + [ ({hooks: [{type: "command", command: $cmd, timeout: 5}]}
                         + (if $m == "" then {} else {matcher: $m} end)) ] end)' "$file")
  if ! printf '%s\n' "$out" | cmp -s - "$file"; then
    [[ -e $file.before-claude-hub ]] || cp -p "$file" "$file.before-claude-hub"
    printf '%s\n' "$out" | write_if_changed "$file" || true
    say "  hook $event registered in $(basename "$(dirname "$file")")"
  fi
}
# Set an account's statusLine to a command, only when it has none (one you set up yourself is never replaced).
add_statusline() {
  local file=$1 cmd=$2
  [[ -f $file ]] || return 0
  if (( DRY )); then echo "  + statusLine: $cmd in $file"; return 0; fi
  jq -e . "$file" >/dev/null 2>&1 || return 0
  if jq -e '.statusLine' "$file" >/dev/null 2>&1; then
    jq -e --arg c "$cmd" '.statusLine.command == $c' "$file" >/dev/null 2>&1 \
      || say "  statusLine in $(basename "$(dirname "$file")") is already set: left alone (the hub's meters need it to run $cmd)"
    return 0
  fi
  local out
  out=$(jq --arg cmd "$cmd" '.statusLine = {type: "command", command: $cmd, padding: 0}' "$file")
  [[ -e $file.before-claude-hub ]] || cp -p "$file" "$file.before-claude-hub"
  printf '%s\n' "$out" | write_if_changed "$file" || true
  say "  statusLine set in $(basename "$(dirname "$file")")"
}
if (( AUTOSWAP || DESKTOPS )); then
  step "Claude hooks in each account"
  for d in "${ALL_ACCOUNTS[@]}"; do
    s=$d/settings.json
    (( AUTOSWAP )) && add_hook "$s" SessionStart "" "$BIN_DIR/claude-account-hook"
    (( AUTOSWAP )) && add_hook "$s" StopFailure rate_limit "$BIN_DIR/claude-autoswap hook"
    if (( DESKTOPS )); then
      add_hook "$s" SessionStart "" "$BIN_DIR/claude-agent-screen session-env"
      add_hook "$s" PreToolUse Bash "$BIN_DIR/claude-agent-screen guard"
      # claude-console-strip writes each Claude's state (the hub's states) and its usage meters (autoswap's).
      add_hook "$s" Stop "" "$BIN_DIR/claude-console-strip state done"
      add_hook "$s" SessionEnd "" "$BIN_DIR/claude-console-strip state ended"
      add_hook "$s" PreToolUse AskUserQuestion "$BIN_DIR/claude-console-strip state asking"
      add_hook "$s" UserPromptSubmit "" "$BIN_DIR/claude-console-strip state running"
      add_hook "$s" Notification "" "$BIN_DIR/claude-console-strip state asking"
      add_statusline "$s" "$BIN_DIR/claude-console-strip statusline"
    fi
  done
fi

# ---- 7. toolkit: hooks and skills --------------------------------------------------------------------------
if (( TOOLKIT )); then
  step "Hooks and skills (toolkit)"
  [[ -f $ROOT/toolkit/tool/toolkit.js ]] || die "$ROOT/toolkit/tool/toolkit.js is missing"
  doit mkdir -p "$DATA" "$STORE" "$HOME/.agents" "$DATA/toolkit-backups"
  # A copy, not the clone itself: the toolkit keeps its registry and state next to its files, and the clone stays clean
  # so updating it with git never conflicts. The registry keeps your own projects; everything else comes from the clone.
  doit rsync -a --exclude .state --exclude toolkit.json --exclude .git "$ROOT/toolkit/" "$STORE/"
  if (( DRY )); then
    echo "  + write $STORE/toolkit.json and $HOME/.agents/toolkit-machine.json"
  else
    if [[ -f $STORE/toolkit.json ]]; then
      jq -s --arg home "$HOME" '.[0] as $src | .[1] as $cur
          | $src + {projectsRoot: ($cur.projectsRoot // $home), projects: ($cur.projects // {})}' \
        "$ROOT/toolkit/toolkit.json" "$STORE/toolkit.json" | write_if_changed "$STORE/toolkit.json" || true
    else
      jq --arg home "$HOME" '.projectsRoot = $home | .projects = {}' "$ROOT/toolkit/toolkit.json" >"$STORE/toolkit.json"
    fi
    machine=$HOME/.agents/toolkit-machine.json
    [[ -f $machine ]] || echo '{}' >"$machine"
    jq --arg store "$STORE" --arg bd "$DATA/toolkit-backups" '(. // {}) + {store: $store, backupDir: $bd}' "$machine" \
      | write_if_changed "$machine" || true
  fi
  # Keep the toolkit's Codex delivery out of a home folder that has no Codex.
  codex_env=()
  [[ -d $HOME/.codex ]] || codex_env=(env "CODEX_HOME=$STORE/.state/codex-unused")
  if (( ! DRY && ${#codex_env[@]} )); then
    say "  note: Codex is not installed ($HOME/.codex is missing), so the 'Codex hook bridge skipped' warnings below are expected and harmless"
  fi
  targets=("${ALL_ACCOUNTS[@]}")
  (( ${#targets[@]} )) || targets=("")      # no accounts yet: the default ~/.claude
  for d in "${targets[@]}"; do
    label=${d:-$HOME/.claude}
    say "  syncing hooks and skills into $label"
    if [[ -n $d ]]; then
      doit env "CLAUDE_CONFIG_DIR=$d" "${codex_env[@]}" node "$STORE/tool/toolkit.js" sync --global \
        || say "  warning: the toolkit sync failed for $label"
    else
      # An account folder in this shell's environment must not decide where the default sync goes.
      doit env -u CLAUDE_CONFIG_DIR "${codex_env[@]}" node "$STORE/tool/toolkit.js" sync --global \
        || say "  warning: the toolkit sync failed for $label"
    fi
  done
fi

# ---- 8. Meta+Return ------------------------------------------------------------------------------------------
# Plasma 6 binds a command through a .desktop file and a [services][<file>.desktop] _launch= entry in kglobalshortcutsrc;
# it takes effect at the next login. A key someone already bound is never taken over.
if (( HUB && SHORTCUTS )); then
  step "Shortcut"
  KEYS=Meta+Return
  DESK=claude-hub-window.desktop
  KRC=$HOME/.config/kglobalshortcutsrc
  holder=
  if [[ -f $KRC ]]; then
    holder=$(awk -v key="$KEYS" -v own="[services][$DESK]" '
      /^\[/ { grp = $0; next }
      /^[^=]+=/ {
        name = $0; sub(/=.*/, "", name); val = $0; sub(/^[^=]*=/, "", val); sub(/,.*/, "", val)
        n = split(val, ks, /\\t|\t/)   # KDE writes a second key after a literal backslash-t
        for (i = 1; i <= n; i++) if (ks[i] == key && grp != own) { print name " in " grp; exit }
      }' "$KRC")
  fi
  if [[ -n $holder ]]; then
    say "  skipped the $KEYS shortcut: it is already bound ($holder)."
    say "  To open the hub popup on a key of your own: System Settings > Keyboard > Shortcuts > Add Command, command: $BIN_DIR/claude-window.sh"
  else
    doit mkdir -p "$HOME/.local/share/applications"
    if (( DRY )); then
      echo "  + write $HOME/.local/share/applications/$DESK"
    else
      write_if_changed "$HOME/.local/share/applications/$DESK" <<EOF || true
[Desktop Entry]
Type=Application
Name=Claude hub popup
Comment=Show or hide the Claude hub popup
Exec=$BIN_DIR/claude-window.sh
Icon=utilities-terminal
Terminal=false
NoDisplay=true
EOF
    fi
    already=0
    if [[ -f $KRC ]] && grep -qxF "[services][$DESK]" "$KRC"; then already=1; fi
    if (( already )); then
      say "  $KEYS already opens the hub popup"
    else
      doit kwriteconfig6 --file kglobalshortcutsrc --group services --group "$DESK" --key _launch "$KEYS"
      say "  $KEYS will open the hub popup after your next login"
    fi
  fi
fi

# ---- done ----------------------------------------------------------------------------------------------------
step "Done"
if (( DRY )); then
  say "That was a dry run: nothing was changed."
  exit 0
fi
if (( ${#ALL_ACCOUNTS[@]} )); then
  say "Sign in to each Claude subscription once. For each of these, run the command, then type /login in Claude and"
  say "sign in with the subscription you want to call by that name:"
  for d in "${ALL_ACCOUNTS[@]}"; do
    say
    say "  CLAUDE_CONFIG_DIR=~/.claude-accounts/$(basename "$d") claude"
    say "  then: /login"
  done
  say
fi
say "Then try: claude-hub   (the popup)   |   claude-autoswap status   |   claude-desks list"
say "Log out and back in once, so Meta+Return and the background services pick everything up."
say "Run ./install.sh again after a git pull to update."
