#!/usr/bin/env bash
set -euo pipefail
plugin=${1:-}
[[ $plugin =~ ^[A-Za-z0-9._-]+$ ]] || { echo "usage: reload.sh <plugin id> [--start]" >&2; exit 2; }
start=0
[[ ${2:-} == --start ]] && start=1
pkg=${RELOAD_PACKAGE:-$HOME/.local/share/kwin/scripts/$plugin}
base=${XDG_DATA_HOME:-$HOME/.local/share}/claude-hub/kwin-reload
K=org.kde.KWin
S=org.kde.kwin.Scripting

[[ -f $pkg/metadata.json ]] || { echo "reload: no KWin script package at $pkg" >&2; exit 2; }
kind=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("X-Plasma-API", ""))' "$pkg/metadata.json")
case $kind in           # the files KWin itself loads (scripting.cpp queryScriptsToLoad)
  javascript) src=$pkg/contents/code/main.js; load=loadScript; node --check "$src";;
  declarativescript) src=$pkg/contents/ui/main.qml; load=loadDeclarativeScript
    [[ ! -f $pkg/contents/code/main.mjs ]] || node --check "$pkg/contents/code/main.mjs";;
  *) echo "reload: $plugin is not a javascript or declarativescript package ('$kind')" >&2; exit 2;;
esac
[[ -f $src ]] || { echo "reload: $src is missing" >&2; exit 2; }
loaded() { qdbus6 $K /Scripting $S.isScriptLoaded "$plugin"; }
running=0
[[ $(loaded) == true ]] && running=1
(( running || start )) || { echo "reload: $plugin is not loaded, so there is nothing to reload (--start loads it)" >&2; exit 1; }

numbers() { { qdbus6 $K /Scripting org.freedesktop.DBus.Introspectable.Introspect || true; } | sed -n 's|^ *<node name="Script\([0-9][0-9]*\)"/>$|\1|p' | sort -n; }
unload() {            # the script goes on KWin's next turn (deleteLater): wait for it
  local i
  qdbus6 $K /Scripting $S.unloadScript "$plugin" >/dev/null
  for ((i = 0; i < 50; i++)); do [[ $(loaded) == false ]] && return 0; sleep 0.1; done
  return 1
}
pad() {               # prints the number the placeholder took
  printf '%s\n' "// Loaded, never run, never unloaded: it holds a KWin script number another script" \
    "// already answers on, so the next script gets a free one (kwin/reload.sh)." > "$base/pad.js"
  qdbus6 $K /Scripting $S.loadScript "$base/pad.js" "claude-script-pad-$(date +%s%N)"
}

mkdir -p "$base/$plugin"
if [[ $kind == declarativescript ]]; then
  copy=$(mktemp -d "$base/$plugin/$(date +%Y%m%d-%H%M%S).XXXXXX")     # a path KWin has never loaded, even twice a second
  rsync -a --exclude '*.before-*' --exclude '*.orig-*' --exclude 'LOCAL-PATCH.md' "$pkg/" "$copy/"
  src=$copy/contents/ui/main.qml
fi

if (( running )); then
  unload || { echo "reload: $plugin did not unload; nothing else was changed" >&2; exit 1; }
fi
trap 'echo "reload: a step failed at line $LINENO after $plugin was unloaded: it may NOT be running — log out and in" >&2' ERR
sid=
for ((try = 0; try < 8; try++)); do
  taken=$(numbers)
  got=$(qdbus6 $K /Scripting $S.$load "$src" "$plugin")
  [[ $got =~ ^[0-9]+$ ]] || { echo "reload: KWin would not load $src ($got): $plugin is NOT running — log out and in" >&2; exit 1; }
  if ! grep -qx "$got" <<<"$taken"; then sid=$got; break; fi
  unload && pad >/dev/null || break
done
[[ -n $sid ]] || { echo "reload: no free script number: $plugin is NOT running — log out and in" >&2; exit 1; }
qdbus6 $K "/Scripting/Script$sid" org.kde.kwin.Script.run >/dev/null
trap - ERR               # it runs; what follows only mends the count
max=$(numbers | tail -1) || max=$sid
next=$((sid + 1))
for ((try = 0; try < 20 && next <= max; try++)); do
  p=$(pad) || break
  [[ $p =~ ^[0-9]+$ ]] || break
  next=$((p + 1))
done
shopt -s nullglob
copies=("$base/$plugin"/[0-9]*-[0-9]*.*)
shopt -u nullglob
if (( ${#copies[@]} > 3 )); then
  for old in "${copies[@]:0:${#copies[@]}-3}"; do rm -rf -- "$old"; done
fi
echo "reload: $plugin runs $src as script $sid"
