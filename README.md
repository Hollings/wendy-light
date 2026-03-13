# Claude Code Discord Bot (Lite)

Minimal Discord bot that gives each channel a persistent Claude Code CLI session. Claude reads messages and responds via an internal HTTP API -- no custom AI code, just the official CLI.

Forked from [wendy-v2](https://github.com/Hollings/wendy) with all the personality, background tasks, site deploys, and other features stripped out.

## How it works

```
Discord message
  -> Bot caches message to SQLite
  -> Spawns `claude` CLI subprocess (--resume SESSION_ID)
  -> Sends nudge via stdin: "you have new messages, call check_messages"
  -> Claude calls internal API:
       GET  /api/check_messages/:id   -> reads messages from SQLite
       POST /api/send_message         -> bot sends Discord message
  -> Session persists across restarts
```

## Setup

1. **Create a Discord bot** at https://discord.com/developers with Message Content Intent enabled

2. **Install Claude Code CLI** in your environment (or use Docker)

3. **Configure:**
   ```bash
   cp .env.example .env
   # Edit .env with your DISCORD_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, and CHANNEL_CONFIG
   ```

4. **Run with Docker:**
   ```bash
   docker compose -f deploy/docker-compose.yml up --build
   ```

   Or locally:
   ```bash
   pip install -r requirements.txt
   python -m wendy
   ```

5. **Auth the CLI** (first time only):
   ```bash
   docker exec -it claude-discord-bot claude login
   ```

## Channel Config

Set `CHANNEL_CONFIG` as a JSON array:

```json
[
  {"id": "123456789", "name": "general", "mode": "full", "model": "sonnet"},
  {"id": "987654321", "name": "coding", "mode": "full", "model": "opus"}
]
```

- `id`: Discord channel ID
- `name`: workspace folder name
- `mode`: `"full"` (all tools) or `"chat"` (restricted)
- `model`: `"opus"`, `"sonnet"`, or `"haiku"`

## Bot Commands

| Command | Description |
|---------|-------------|
| `!clear` | Reset the current Claude session |
| `!resume <id>` | Resume a previous session by ID |
| `!session` | Show current session info |
| `!system` | Upload the assembled system prompt |
| `!version` | Show the running git commit |

Type the bot's name in ALL CAPS to interrupt a running generation.

## Customization

- **System prompt**: Edit `config/system_prompt.txt` -- `{bot_name}` and `{folder}` are replaced at runtime
- **Claude settings/hooks**: Edit `config/claude_settings.json`
- **Bot name**: Set `BOT_NAME` env var (used for interrupt keyword)

## Architecture

```
wendy/
  __main__.py        -- entry point
  discord_client.py  -- message routing, generation lifecycle
  cli.py             -- Claude CLI subprocess management
  prompt.py          -- system prompt assembly
  api_server.py      -- internal HTTP API (aiohttp)
  state.py           -- SQLite state manager
  sessions.py        -- session lifecycle
  config.py          -- configuration parsing
  models.py          -- data structures
  paths.py           -- filesystem paths
```

Single Python process, single container. ~1,500 lines of Python.
