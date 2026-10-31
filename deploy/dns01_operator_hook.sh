#!/bin/sh
# Manual DNS-01 rendezvous for the approved canonical hostname only.
# No DNS credentials are stored. An operator must publish and verify the exact
# TXT value, then put that same value in ready. This is NOT autorenewal.
set -eu
umask 077
[ "$#" = 2 ] || exit 2
action=$1
state=$2
[ "${CERTBOT_DOMAIN:-}" = ipfs.yataverse.com ] || exit 2
value=${CERTBOT_VALIDATION:-}
[ "${#value}" = 43 ] || exit 2
case "$value" in *[!A-Za-z0-9_-]*) exit 2 ;; esac
[ -d "$state" ] && [ ! -L "$state" ] || exit 2
case "$action" in
auth)
    printf '%s\n' "$value" > "$state/value.tmp"
    mv "$state/value.tmp" "$state/value"
    # A stale acknowledgement for another authorization cannot release this one.
    stop=$(( $(date +%s) + 300 ))
    while ! cmp -s "$state/value" "$state/ready"; do
        [ "$(date +%s)" -lt "$stop" ] || exit 1
        sleep 1
    done
    ;;
cleanup)
    if [ -f "$state/value" ] && [ "$(cat "$state/value")" = "$value" ]; then
        rm -f "$state/value" "$state/ready"
    fi
    ;;
*) exit 2 ;;
esac
