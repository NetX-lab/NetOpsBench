#!/bin/sh
set -eu
# Install the namespace policy before dropping every capability. The agent
# cannot alter this policy or reach another host/lab endpoint.
iptables -P OUTPUT DROP
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -p tcp -d "$DIAGNOSTIC_GATEWAY" --dport "$DIAGNOSTIC_MCP_PORT" -j ACCEPT
iptables -A OUTPUT -p tcp -d "$DIAGNOSTIC_GATEWAY" --dport "$DIAGNOSTIC_PROXY_PORT" -j ACCEPT
ip6tables -P OUTPUT DROP
chown "$DIAGNOSTIC_UID:$DIAGNOSTIC_GID" /workspace /tmp /home/diagnostic/.codex
exec setpriv --reuid="$DIAGNOSTIC_UID" --regid="$DIAGNOSTIC_GID" --clear-groups \
    --bounding-set=-all --inh-caps=-all --ambient-caps=-all --no-new-privs "$@"
