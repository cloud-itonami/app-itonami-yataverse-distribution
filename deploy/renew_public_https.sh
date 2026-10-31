#!/bin/sh
# Existing Xavier HTTPS ingress repair; dry-run unless explicitly applied.
# An operator must approve the current WAN address, never discover-and-accept it.
set -eu

apply=false
if [ "${1:-}" = --apply ]; then apply=true; shift; fi
if [ "$#" != 1 ]; then
    echo 'usage: renew_public_https.sh [--apply] REVIEWED_WAN_IPV4' >&2
    exit 2
fi
expected_wan=$1
case "$expected_wan" in
    ''|*[!0-9.]*) echo 'refuse: invalid reviewed IPv4' >&2; exit 2 ;;
esac
printf '%s\n' "$expected_wan" | awk -F. '
    NF != 4 {exit 1}
    {for (i=1;i<=4;i++) if ($i == "" || $i+0 > 255) exit 1}
' || { echo 'refuse: invalid reviewed IPv4' >&2; exit 2; }

refuse() { echo "refuse: $*" >&2; exit 2; }
lan_ip=$(ip -4 -o addr show dev eth0 scope global |
    awk 'NR==1 {split($4,p,"/"); print p[1]}')
[ "$lan_ip" = 192.168.1.28 ] || refuse 'Xavier LAN address differs'
systemctl is-active --quiet nginx || refuse 'nginx is inactive'
nginx -t || refuse 'nginx configuration is invalid'

# Bound UPnP discovery/enumeration; never parse a failed or partial listing.
listing=$(timeout 45 upnpc -u http://192.168.1.1:49152/gatedesc.xml -l) ||
    refuse 'router inventory unavailable'
actual_wan=$(printf '%s\n' "$listing" |
    awk -F ' = ' '/^ExternalIPAddress = / {print $2}')
[ "$actual_wan" = "$expected_wan" ] || refuse 'router public IPv4 differs from reviewed address'
existing=$(printf '%s\n' "$listing" |
    awk '$2 == "TCP" && $3 ~ /^443->/ {print $3}')
case "$existing" in
    ''|'443->192.168.1.28:443') ;;
    *) refuse 'TCP 443 has another or ambiguous owner' ;;
esac

# This checks the existing dated Reader through its already-installed SNI vhost.
# It is a health preflight, not proof of block custody or canonical TLS.
health=$(curl --noproxy '*' --fail --silent --show-error --connect-timeout 2 \
    --max-time 5 --resolve yataverse-data.220-146-170-114.sslip.io:443:127.0.0.1 \
    https://yataverse-data.220-146-170-114.sslip.io/health) || refuse 'Reader TLS health failed'
printf '%s' "$health" | grep -F 'f616962875a0850efa824b53be45fc22edce39f4c280a32cb80b72a55b020188' >/dev/null ||
    refuse 'Reader inventory differs'

if [ "$apply" = false ]; then
    echo 'ready: reviewed WAN, existing owner, nginx and Reader health checked; no mapping changed'
    exit 0
fi
result=$(timeout 30 upnpc -u http://192.168.1.1:49152/gatedesc.xml \
    -a "$lan_ip" 443 443 TCP 900) || refuse 'mapping renewal failed'
printf '%s\n' "$result" |
    grep -F "external $expected_wan:443 TCP is redirected to internal $lan_ip:443" >/dev/null ||
    refuse 'mapping acknowledgement differs; inspect router before retry'
# Re-read ownership instead of treating the command acknowledgement as readback.
after=$(timeout 45 upnpc -u http://192.168.1.1:49152/gatedesc.xml -l) ||
    refuse 'mapping readback unavailable; inspect router'
after_wan=$(printf '%s\n' "$after" | awk -F ' = ' '/^ExternalIPAddress = / {print $2}')
after_owner=$(printf '%s\n' "$after" | awk '$2 == "TCP" && $3 ~ /^443->/ {print $3}')
[ "$after_wan" = "$expected_wan" ] && [ "$after_owner" = '443->192.168.1.28:443' ] ||
    refuse 'mapping readback differs; inspect router'
echo 'renewed: existing Xavier TCP 443 lease confirmed; canonical HTTPS and external CID readback still required'
