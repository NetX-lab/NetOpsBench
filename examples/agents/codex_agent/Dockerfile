FROM alpine:3.24.1
RUN apk add --no-cache python3 ca-certificates iptables util-linux bash \
    && adduser -D -u 1000 diagnostic
COPY codex /usr/local/bin/codex
COPY codex-code-mode-host /usr/local/bin/codex-code-mode-host
COPY entrypoint.sh /usr/local/bin/diagnostic-entrypoint
RUN chmod 755 /usr/local/bin/diagnostic-entrypoint /usr/local/bin/codex /usr/local/bin/codex-code-mode-host
WORKDIR /workspace
ENV HOME=/home/diagnostic
ENTRYPOINT ["/usr/local/bin/diagnostic-entrypoint"]
