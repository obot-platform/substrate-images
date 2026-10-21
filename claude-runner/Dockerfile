FROM debian:bookworm-slim
ARG CLAUDE_CODE_VERSION=2.1.303
ARG GO_VERSION=1.27.3
ARG NODE_VERSION=24.22.1
ARG YQ_VERSION=4.54.1
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip python3-venv git curl ca-certificates \
        openssh-client jq xz-utils build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN case "$TARGETARCH" in amd64) arch=x64 ;; arm64) arch=arm64 ;; *) exit 1 ;; esac \
    && curl -fsSL "https://downloads.claude.ai/claude-code-releases/${CLAUDE_CODE_VERSION}/linux-${arch}/claude" -o /usr/local/bin/claude \
    && chmod +x /usr/local/bin/claude \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${arch}.tar.xz" \
        | tar -xJ -C /usr/local --strip-components=1 --no-same-owner \
    && curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" | tar -xz -C /usr/local \
    && curl -fsSL "https://github.com/mikefarah/yq/releases/download/v${YQ_VERSION}/yq_linux_${TARGETARCH}" -o /usr/local/bin/yq \
    && chmod +x /usr/local/bin/yq
ENV PATH=/root/go/bin:/usr/local/go/bin:$PATH
RUN git config --system user.name Claude \
    && git config --system user.email noreply@anthropic.com
COPY runner.py /opt/obot/runner.py
COPY hooks /opt/obot/hooks
COPY CLAUDE.md /root/.claude/CLAUDE.md
RUN chmod 755 /opt/obot/hooks/*
ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 80
ENTRYPOINT ["python3", "/opt/obot/runner.py"]
CMD ["supervise"]
