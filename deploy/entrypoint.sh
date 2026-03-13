#!/bin/bash
# Entrypoint: setup permissions, then run main command

git config --global --add safe.directory /app

# Mark CLI onboarding complete
echo '{"hasCompletedOnboarding": true}' > /root/.claude.json
if [ ! -f /home/wendy/.claude.json ]; then
    echo '{"hasCompletedOnboarding": true}' > /home/wendy/.claude.json
fi
chown wendy:wendy /home/wendy/.claude.json

# Git credentials for wendy user
HOME=/home/wendy git config --global --add safe.directory /app
chown wendy:wendy /home/wendy/.gitconfig 2>/dev/null || true

# CLI subprocess isolation: wendy user (UID 1000)
chmod 711 /root
ln -sfn /root/.claude /home/wendy/.claude

# Ensure base directories
mkdir -p /data/wendy/channels /data/wendy/shared

# Remove MCP auth cache (blocks headless CLI)
rm -f /root/.claude/mcp-needs-auth-cache.json

# Writable areas: owned by wendy
chown -R wendy:wendy /root/.claude/ 2>/dev/null || true
chown -R wendy:wendy /data/wendy/channels/ 2>/dev/null || true
chown -R wendy:wendy /data/wendy/shared/ 2>/dev/null || true

exec "$@"
