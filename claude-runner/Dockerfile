FROM debian:bookworm-slim
ARG CLAUDE_CODE_VERSION=2.1.303
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends python3 git curl ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/*
RUN case "$TARGETARCH" in amd64) arch=x64 ;; arm64) arch=arm64 ;; *) exit 1 ;; esac \
    && curl -fsSL "https://downloads.claude.ai/claude-code-releases/${CLAUDE_CODE_VERSION}/linux-${arch}/claude" -o /usr/local/bin/claude \
    && chmod +x /usr/local/bin/claude
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
