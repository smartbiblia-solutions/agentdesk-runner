# agentdesk-runner

**Execution plane** for the AgentDesk control plane.

```
AgentDesk (desktop/web app)
    └─► agentdesk-runner  (this repo)
            └─► nanobot agent -m "..."
```

## How it works

1. AgentDesk **builds a bundle** (zip: `agent.md`, `skills/`, `tools.yaml`)
2. AgentDesk **POSTs the bundle** to `POST /bundles`
3. AgentDesk **starts a run** via `POST /runs {run_id, bundle_id, prompt, skill}`
4. The runner unpacks the bundle, writes `~/.nanobot/config.json` from the
   agent config, then executes `nanobot agent -m "<prompt>"`
5. Output streams back to AgentDesk via `WS /ws/runs/{run_id}`

**Secrets (API keys) are never in the bundle.** They live in the runner's
own `~/.nanobot/config.json` or as environment variables.

## First version Install app requirements (including nanobot)

```
pip install -r requirements.txt
```

Configure nanobot

```
vi ~/.nanobot/config.json
```

Run

```
uvicorn runner.main:app --host 0.0.0.0 --port 8000
```

Check: https://xxxxx-8000.app.github.dev/health

Examples

Make a POST request on /run with

```
{
  "cmd": "nanobot agent -m 'list files in project'"
}
```

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure nanobot

Run once to initialise:

```bash
nanobot onboard
```

Edit `~/.nanobot/config.json` and add your API key:

```json
{
  "providers": {
    "openrouter": { "apiKey": "sk-or-v1-..." }
  },
  "agents": {
    "defaults": { "model": "anthropic/claude-opus-4-5" }
  }
}
```

The runner **merges** model/agent settings from each bundle into this file
before each run. Your API keys and personal settings are always preserved.

### 3. Start

```bash
uvicorn runner.main:app --host 0.0.0.0 --port 8000
```

### 4. Verify

```
GET http://localhost:8000/health
```

## API

| Method | Path | Body / Notes |
|--------|------|--------------|
| GET | `/health` | returns `{status, runner_version, nanobot}` |
| POST | `/bundles` | multipart: `bundle`=zip file, `manifest`=JSON string |
| POST | `/runs` | `{run_id, bundle_id, prompt, skill?}` |
| GET | `/runs/{id}` | `{run_id, status, exit_code, started_at, ended_at}` |
| DELETE | `/runs/{id}` | cancel (SIGTERM) |
| GET | `/runs/{id}/logs` | `[{stream, text, ts}]` |
| WS | `/ws/runs/{id}` | streaming `{type, run_id, text, ts}` events |

## AgentDesk config (`~/.agentdesk/config.yaml`)

```yaml
runners:
  local-wsl:
    type: agentdesk_runner
    host: localhost
    port: 8000

  # GitHub Codespace:
  # codespace:
  #   type: agentdesk_runner
  #   host: xxxxx-8000.app.github.dev
  #   port: 443
```
