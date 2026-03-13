# Claude Code Discord Bot (Lite)

A minimal Discord bot that gives each channel a persistent [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) session. No AI SDK, no API keys beyond your Claude Code subscription -- just the official CLI running headless inside a Docker container, talking to Discord through a lightweight Python bridge.

Stripped-down fork of [wendy](https://github.com/Hollings/wendy). ~1,700 lines of Python.

## How it works

```
Discord message arrives
  -> Bot caches it to SQLite
  -> Spawns `claude` CLI subprocess (--resume SESSION_ID)
  -> Sends nudge via stdin: "new messages, call check_messages"
  -> Claude calls the bot's internal HTTP API:
       GET  /api/check_messages/:id   -> reads messages from SQLite
       POST /api/send_message         -> bot sends a Discord message
  -> Session JSONL persists on disk across restarts
```

Claude never responds through stdout. All user-visible messages go through the internal API, which means the bot controls delivery (attachments, replies, reactions, message splitting).

## Features

- **Persistent sessions** -- each channel gets a Claude CLI session that survives restarts. Conversation history lives in JSONL files managed by the CLI.
- **Thread support** -- Discord threads automatically fork the parent channel's session, inheriting its full context.
- **Interrupt** -- type the bot's name in ALL CAPS to cancel a running generation and start fresh.
- **New-message interrupt** -- if someone sends a message while Claude is composing a reply, the API forces Claude to re-read before sending, so responses are never stale.
- **Timeout auto-continuation** -- if the CLI hits the idle/runtime timeout, the bot automatically re-invokes so long-running work isn't lost (up to 2 retries).
- **Attachment handling** -- uploaded files are saved to the channel's workspace and their paths appear in `check_messages`, so Claude can `Read` them.
- **Emoji reactions** -- Claude can add reactions to messages via the batch action API.
- **Session commands** -- `!clear`, `!resume`, `!session`, `!system`, `!version`.

## Requirements

- A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) subscription (Max or Team plan)
- Docker (recommended) or Python 3.12+ with the Claude CLI installed
- A Discord bot token with Message Content Intent enabled

## Quick start

### 1. Create a Discord bot

Go to https://discord.com/developers/applications, create an app, add a bot, and enable the **Message Content** intent under Bot settings. Invite it to your server with the `bot` scope and `Send Messages` + `Read Message History` + `Add Reactions` + `Attach Files` permissions.

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```bash
DISCORD_TOKEN=your_discord_bot_token
CLAUDE_CODE_OAUTH_TOKEN=your_claude_code_oauth_token
CHANNEL_CONFIG='[{"id":"123456789","name":"general","mode":"full"}]'
```

To get the OAuth token, run this on a machine where you're already logged into Claude Code:

```bash
claude setup-token
```

This prints a `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...` line you can paste directly into `.env`.

To find a channel ID, enable Developer Mode in Discord settings, then right-click a channel and click "Copy Channel ID".

### 3. Run

```bash
docker compose -f deploy/docker-compose.yml up --build
```

### 4. Authenticate the CLI (first time only)

```bash
docker exec -it claude-discord-bot claude login
```

After this, the bot should respond to messages in your configured channels.

## Channel config

Set `CHANNEL_CONFIG` as a JSON array in your `.env`:

```json
[
  {"id": "123456789", "name": "general", "mode": "full", "model": "sonnet"},
  {"id": "987654321", "name": "coding", "mode": "full", "model": "opus"}
]
```

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Discord channel ID |
| `name` | yes | Workspace folder name (alphanumeric, hyphens, underscores) |
| `mode` | no | `"full"` (default, all tools) or `"chat"` (restricted file access) |
| `model` | no | `"opus"`, `"sonnet"` (default), or `"haiku"` |

## Bot commands

| Command | Description |
|---------|-------------|
| `!clear` | Reset the current Claude session (archives the old one) |
| `!resume <id>` | Resume a previous session by ID or prefix |
| `!session` | Show current session ID, start time, turn count, and token usage |
| `!system` | Upload the assembled system prompt as a text file |
| `!version` | Show the running git commit |

Type the bot's name in ALL CAPS (e.g. `BOT`) to interrupt a running generation.

## Customization

### System prompt

Edit `config/system_prompt.txt`. Two placeholders are replaced at runtime:

- `{bot_name}` -- the `BOT_NAME` env var (default: `Bot`)
- `{folder}` -- the channel's workspace folder name

### Claude settings and hooks

Edit `config/claude_settings.json` to configure Claude Code hooks (PreToolUse, PostToolUse, Stop, etc.). See the [Claude Code hooks docs](https://docs.anthropic.com/en/docs/claude-code/hooks) for the full schema.

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_TOKEN` | required | Discord bot token |
| `CLAUDE_CODE_OAUTH_TOKEN` | required | Claude Code auth token |
| `CHANNEL_CONFIG` | required | JSON array of channel configs |
| `BOT_NAME` | `Bot` | Display name (also the interrupt keyword) |
| `WENDY_PROXY_PORT` | `8945` | Port for the internal HTTP API |
| `CLAUDE_CLI_IDLE_TIMEOUT` | `300` | Seconds of no CLI output before timeout |
| `CLAUDE_CLI_MAX_RUNTIME` | `1800` | Absolute max CLI runtime in seconds |
| `LOG_LEVEL` | `INFO` | Python log level |

## Architecture

```
wendy/
  __main__.py        -- entry point
  discord_client.py  -- Discord event handling, generation lifecycle
  cli.py             -- Claude CLI subprocess spawning and streaming
  prompt.py          -- system prompt assembly
  api_server.py      -- internal HTTP API (aiohttp on localhost)
  state.py           -- SQLite state (messages, sessions, threads)
  sessions.py        -- session create/resume/reset
  config.py          -- env var parsing, model map
  models.py          -- dataclasses
  paths.py           -- filesystem path helpers
```

Single Python process, single Docker container. The CLI subprocess runs as a non-root `wendy` user for isolation.

## Data layout (inside Docker)

```
/data/wendy/
  shared/
    wendy.db             -- SQLite: messages, sessions, threads
  channels/
    general/             -- per-channel workspace
      attachments/       -- downloaded Discord files
      .claude/           -- Claude Code settings (synced from /app/config)
/root/.claude/
  projects/              -- CLI session JSONL files
```

## Running without Docker

```bash
pip install -r requirements.txt
# Make sure `claude` CLI is in your PATH
export DISCORD_TOKEN=...
export CLAUDE_CODE_OAUTH_TOKEN=...
export CHANNEL_CONFIG='[{"id":"123","name":"test","mode":"full"}]'
python -m wendy
```

Note: without Docker, there's no user isolation for the CLI subprocess. It runs as your current user.
