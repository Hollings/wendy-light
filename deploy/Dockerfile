# Claude Code Discord Bot - Lite
FROM python:3.12-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git jq sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Node.js + Claude Code CLI
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Non-root user for CLI subprocess isolation
RUN useradd -m -s /bin/bash -u 1000 wendy

# Copy application code
COPY . .

# Entrypoint
RUN chmod +x /app/deploy/entrypoint.sh
ENTRYPOINT ["/app/deploy/entrypoint.sh"]

CMD ["python", "-m", "wendy"]
