#!/bin/sh
if [ -x /usr/local/bin/bun ]; then
  case "$(export -p)" in
    *"export NODE_"*|*"export BUN_"*) ;;          # any engine variable: node, exactly as today
    *) /usr/local/bin/bun --no-env-file --config=/dev/null --no-install "$@"
       s=$?; [ "$s" -ne 126 ] && [ "$s" -ne 127 ] && exit "$s" ;;
  esac
fi
node "$@"
