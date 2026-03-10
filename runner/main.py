"""
agentdesk-runner — Execution Plane

Responsibilities:
  • Receive execution bundles from AgentDesk (control plane)
  • Unpack bundle into a temp workspace
  • Translate agent.md + tools.yaml → ~/.nanobot/config.json
  • Execute: nanobot agent -m "<prompt>"
  • Stream stdout/stderr line-by-line via WebSocket
  • Expose run lifecycle: status, cancel, logs

API:
  GET    /health
  POST   /bundles          multipart/form-data: bundle=<zip>, manifest=<json>
  POST   /runs             {run_id, bundle_id, prompt, skill}
  GET    /runs/{run_id}    → {run_id, status, exit_code, started_at, ended_at}
  DELETE /runs/{run_id}    cancel
  GET    /runs/{id}/logs   → [{stream, text, ts}]
  WS     /ws/runs/{id}     streaming log events

Nanobot is invoked as:
  nanobot agent -m "<prompt>"
  (working directory = unpacked bundle workspace)
  (config.json written from agent.md frontmatter before each run)

Start:
  uvicorn runner.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
log = logging.getLogger('agentdesk-runner')

app = FastAPI(title='agentdesk-runner', version='2.0.0')

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'], allow_credentials=True,
    allow_methods=['*'], allow_headers=['*'],
)

# ── Directories ───────────────────────────────────────────────────────────────

BUNDLES_DIR = Path(os.environ.get('RUNNER_BUNDLES_DIR',
                                   Path.home() / '.agentdesk-runner' / 'bundles'))
NANOBOT_CONFIG = Path(os.environ.get('NANOBOT_CONFIG',
                                      Path.home() / '.nanobot' / 'config.json'))

BUNDLES_DIR.mkdir(parents=True, exist_ok=True)
NANOBOT_CONFIG.parent.mkdir(parents=True, exist_ok=True)


# ── In-memory state ───────────────────────────────────────────────────────────
# In production this would be a database, but for the runner an in-process
# dict is sufficient since the runner is single-machine.

_bundles: dict[str, dict]  = {}   # bundle_id → {manifest, path}
_runs:    dict[str, dict]  = {}   # run_id    → run record
_logs:    dict[str, list]  = {}   # run_id    → [LogLine dicts]
_procs:   dict[str, Any]   = {}   # run_id    → asyncio.subprocess.Process
_ws_channels: dict[str, list[WebSocket]] = {}   # run_id → [WebSocket]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# Health
# ══════════════════════════════════════════════════════════════════════════════

@app.get('/health')
def health():
    nanobot_ok = shutil.which('nanobot') is not None
    return {
        'status':         'ok',
        'runner_version': '2.0.0',
        'nanobot':        'available' if nanobot_ok else 'not found',
    }


# ══════════════════════════════════════════════════════════════════════════════
# Bundles  —  POST /bundles
# ══════════════════════════════════════════════════════════════════════════════

@app.post('/bundles', status_code=201)
async def receive_bundle(
    bundle:   UploadFile = File(..., description='Bundle zip archive'),
    manifest: str        = Form(..., description='JSON manifest string'),
):
    """
    Receive and unpack an execution bundle from AgentDesk.

    Stores the unpacked bundle in BUNDLES_DIR/<bundle_id>/.
    Does NOT start a run — call POST /runs for that.
    """
    try:
        meta = json.loads(manifest)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f'Invalid manifest JSON: {e}')

    bundle_id = meta.get('bundle_id') or str(uuid.uuid4())
    bundle_path = BUNDLES_DIR / bundle_id

    # Clean up any previous bundle with the same id
    if bundle_path.exists():
        shutil.rmtree(bundle_path)
    bundle_path.mkdir(parents=True)

    # Unpack zip
    zip_bytes = await bundle.read()
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(bundle_path)
    except zipfile.BadZipFile as e:
        shutil.rmtree(bundle_path)
        raise HTTPException(400, f'Invalid zip archive: {e}')

    _bundles[bundle_id] = {
        'bundle_id':  bundle_id,
        'path':       str(bundle_path),
        'manifest':   meta,
        'received_at': _now(),
    }

    log.info(f'Bundle {bundle_id} unpacked to {bundle_path} ({len(zip_bytes):,} bytes)')
    return {
        'bundle_id': bundle_id,
        'path':      str(bundle_path),
        'files':     [str(p.relative_to(bundle_path)) for p in bundle_path.rglob('*') if p.is_file()],
    }


# ══════════════════════════════════════════════════════════════════════════════
# Runs  —  POST /runs, GET /runs/{id}, DELETE /runs/{id}
# ══════════════════════════════════════════════════════════════════════════════

class RunCreate(BaseModel):
    run_id:    str
    bundle_id: str
    prompt:    str
    skill:     str | None = None


@app.post('/runs', status_code=201)
async def create_run(body: RunCreate):
    """
    Start execution of a previously-received bundle.

    Workflow:
      1. Look up the unpacked bundle dir
      2. Write ~/.nanobot/config.json from agent.md frontmatter
      3. Compose the task prompt (prepend skill body if given)
      4. Spawn: nanobot agent -m "<prompt>"  (cwd = bundle workspace)
      5. Stream output → WS /ws/runs/{run_id}
    """
    if body.bundle_id not in _bundles:
        raise HTTPException(404, f'Bundle {body.bundle_id!r} not found. '
                                  f'Call POST /bundles first.')

    if body.run_id in _runs:
        raise HTTPException(409, f'Run {body.run_id} already exists')

    bundle_path = Path(_bundles[body.bundle_id]['path'])

    # Build run record
    ts = _now()
    _runs[body.run_id] = {
        'run_id':     body.run_id,
        'bundle_id':  body.bundle_id,
        'prompt':     body.prompt,
        'skill':      body.skill,
        'status':     'running',
        'exit_code':  None,
        'started_at': ts,
        'ended_at':   None,
    }
    _logs[body.run_id] = []
    _ws_channels[body.run_id] = []

    # Fire-and-forget execution
    asyncio.create_task(
        _execute_run(body.run_id, bundle_path, body.prompt, body.skill)
    )

    return {
        **_runs[body.run_id],
        'ws_url': f'/ws/runs/{body.run_id}',
    }


@app.get('/runs/{run_id}')
def get_run(run_id: str):
    if run_id not in _runs:
        raise HTTPException(404)
    return _runs[run_id]


@app.delete('/runs/{run_id}')
async def cancel_run(run_id: str):
    if run_id not in _runs:
        raise HTTPException(404)
    proc = _procs.get(run_id)
    if proc:
        try:
            if sys.platform != 'win32':
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except (ProcessLookupError, Exception) as e:
            log.warning(f'cancel {run_id}: {e}')

    _runs[run_id].update({'status': 'cancelled', 'ended_at': _now()})
    await _broadcast(run_id, {'type': 'run:cancelled', 'run_id': run_id, 'ts': _now()})
    return {'ok': True}


@app.get('/runs/{run_id}/logs')
def get_run_logs(run_id: str):
    if run_id not in _runs:
        raise HTTPException(404)
    return _logs.get(run_id, [])


# ══════════════════════════════════════════════════════════════════════════════
# WebSocket streaming  —  WS /ws/runs/{run_id}
# ══════════════════════════════════════════════════════════════════════════════

@app.websocket('/ws/runs/{run_id}')
async def ws_run(run_id: str, websocket: WebSocket):
    await websocket.accept()
    if run_id not in _ws_channels:
        _ws_channels[run_id] = []
    _ws_channels[run_id].append(websocket)

    # Replay any logs already captured (for reconnect)
    for entry in _logs.get(run_id, []):
        try:
            await websocket.send_text(json.dumps({
                'type': f'run:{entry["stream"]}',
                'run_id': run_id,
                'text': entry['text'],
                'ts':   entry['ts'],
            }))
        except Exception:
            break

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get('type') == 'ping':
                await websocket.send_text(json.dumps({'type': 'pong'}))
    except WebSocketDisconnect:
        pass
    finally:
        if run_id in _ws_channels:
            _ws_channels[run_id] = [w for w in _ws_channels[run_id] if w is not websocket]


# ══════════════════════════════════════════════════════════════════════════════
# Execution engine
# ══════════════════════════════════════════════════════════════════════════════

async def _execute_run(
    run_id:      str,
    bundle_path: Path,
    prompt:      str,
    skill:       str | None,
) -> None:
    """
    Core execution:
      1. Write nanobot config from agent.md frontmatter
      2. Compose final prompt
      3. Spawn nanobot agent
      4. Stream output
      5. Finalize run record
    """
    try:
        await _system_log(run_id, f'agentdesk-runner: starting run {run_id}')

        # 1. Write ~/.nanobot/config.json
        _write_nanobot_config(bundle_path)
        await _system_log(run_id, 'nanobot config written')

        # 2. Compose prompt
        final_prompt = _compose_prompt(bundle_path, prompt, skill)
        await _system_log(run_id, f'skill: {skill or "none"} | prompt: {final_prompt[:80]}…'
                          if len(final_prompt) > 80 else f'prompt: {final_prompt}')

        # 3. Find workspace dir (bundle_path/workspace if exists, else bundle_path)
        cwd = bundle_path / 'workspace'
        if not cwd.exists():
            cwd = bundle_path

        # 4. Spawn nanobot
        nanobot_bin = shutil.which('nanobot') or 'nanobot'
        cmd = [nanobot_bin, 'agent', '-m', final_prompt]
        log.info(f'[run:{run_id}] cwd={cwd} cmd={cmd}')

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_env(),
        )
        _procs[run_id] = proc

        # 5. Stream
        await asyncio.gather(
            _drain_stream(run_id, proc.stdout, 'stdout'),
            _drain_stream(run_id, proc.stderr, 'stderr'),
        )
        await proc.wait()

    except FileNotFoundError:
        msg = ('nanobot not found. Install with: pip install nanobot-ai  '
               'then run: nanobot onboard')
        log.error(f'[run:{run_id}] {msg}')
        await _system_log(run_id, msg)
        _runs[run_id].update({'status': 'error', 'exit_code': 127, 'ended_at': _now()})
        await _broadcast(run_id, {
            'type': 'run:error', 'run_id': run_id, 'message': msg, 'ts': _now()
        })
        return

    except Exception as e:
        log.exception(f'[run:{run_id}] unexpected error: {e}')
        await _system_log(run_id, f'runner error: {e}')
        _runs[run_id].update({'status': 'error', 'exit_code': 1, 'ended_at': _now()})
        await _broadcast(run_id, {
            'type': 'run:error', 'run_id': run_id, 'message': str(e), 'ts': _now()
        })
        return

    finally:
        _procs.pop(run_id, None)

    # Finalize
    exit_code = proc.returncode if proc.returncode is not None else -1
    status = 'done' if exit_code == 0 else 'error'
    _runs[run_id].update({'status': status, 'exit_code': exit_code, 'ended_at': _now()})

    await _broadcast(run_id, {
        'type': 'run:done', 'run_id': run_id,
        'exit_code': exit_code, 'ts': _now(),
    })
    log.info(f'[run:{run_id}] finished — exit {exit_code}')


async def _drain_stream(
    run_id: str,
    stream: asyncio.StreamReader,
    stream_name: str,
) -> None:
    while True:
        raw = await stream.readline()
        if not raw:
            break
        text = raw.decode('utf-8', errors='replace').rstrip()
        if not text:
            continue
        ts = _now()
        _logs[run_id].append({'stream': stream_name, 'text': text, 'ts': ts})
        await _broadcast(run_id, {
            'type':   f'run:{stream_name}',
            'run_id': run_id,
            'text':   text,
            'ts':     ts,
        })


async def _system_log(run_id: str, text: str) -> None:
    ts = _now()
    _logs[run_id].append({'stream': 'system', 'text': text, 'ts': ts})
    await _broadcast(run_id, {
        'type': 'run:stdout', 'run_id': run_id, 'text': f'[runner] {text}', 'ts': ts,
    })


async def _broadcast(run_id: str, data: dict) -> None:
    dead = []
    for ws in _ws_channels.get(run_id, []):
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            dead.append(ws)
    for ws in dead:
        if run_id in _ws_channels:
            _ws_channels[run_id] = [w for w in _ws_channels[run_id] if w is not ws]


# ══════════════════════════════════════════════════════════════════════════════
# Nanobot config translation
# ══════════════════════════════════════════════════════════════════════════════

def _write_nanobot_config(bundle_path: Path) -> None:
    """
    Translate agent.md frontmatter → ~/.nanobot/config.json.

    AgentDesk agent.md frontmatter:
      name:        my-agent
      model:       anthropic/claude-opus-4-5
      provider:    openrouter          # or openai, anthropic, ollama
      temperature: 0.3
      max_tokens:  4096

    Nanobot config.json schema:
      {
        "agents": {
          "defaults": { "model": "anthropic/claude-opus-4-5" }
        },
        "providers": {
          "openrouter": { "apiKey": "$OPENROUTER_API_KEY" }
        }
      }

    API keys are NEVER in the bundle.  They come from:
      1. The runner's existing ~/.nanobot/config.json  (user pre-configured)
      2. Environment variables injected into the runner process

    This function MERGES with any existing nanobot config so the user's
    API keys and personal settings are preserved.  Only model/agent
    defaults from agent.md are overwritten.
    """
    agent_md = bundle_path / 'agent.md'
    meta = _read_frontmatter(agent_md)
    tools_cfg = _read_tools(bundle_path)
    skills_cfg = _read_skills(bundle_path)

    # Load existing nanobot config (preserves API keys)
    existing: dict = {}
    if NANOBOT_CONFIG.exists():
        try:
            existing = json.loads(NANOBOT_CONFIG.read_text()) or {}
        except json.JSONDecodeError:
            log.warning('Existing ~/.nanobot/config.json is invalid JSON — will overwrite')

    # Build merged config
    config: dict = dict(existing)

    # Model / agent defaults from agent.md
    model = meta.get('model')
    if model:
        config.setdefault('agents', {}).setdefault('defaults', {})['model'] = model

    temperature = meta.get('temperature')
    if temperature is not None:
        config.setdefault('agents', {}).setdefault('defaults', {})['temperature'] = temperature

    max_tokens = meta.get('max_tokens')
    if max_tokens is not None:
        config.setdefault('agents', {}).setdefault('defaults', {})['maxTokens'] = max_tokens

    # Provider (ensure key exists; leave apiKey to existing config / env)
    provider = meta.get('provider', '').lower()
    _PROVIDER_ENV_MAP = {
        'openai':     'OPENAI_API_KEY',
        'anthropic':  'ANTHROPIC_API_KEY',
        'openrouter': 'OPENROUTER_API_KEY',
        'ollama':     None,
        'groq':       'GROQ_API_KEY',
        'mistral':    'MISTRAL_API_KEY',
    }
    if provider and provider in _PROVIDER_ENV_MAP:
        env_key = _PROVIDER_ENV_MAP[provider]
        if 'providers' not in config:
            config['providers'] = {}
        if provider not in config['providers']:
            config['providers'][provider] = {}
        # Only set apiKey from env if not already set in config
        if env_key and env_key not in (config['providers'][provider].get('apiKey', '')):
            env_val = os.environ.get(env_key)
            if env_val and not config['providers'][provider].get('apiKey'):
                config['providers'][provider]['apiKey'] = env_val

    # MCP tools from tools.yaml → nanobot "tools" section (future extension)
    # Currently nanobot handles MCP via its own config; we log what's available.
    if tools_cfg:
        log.info(f'tools.yaml has {len(tools_cfg.get("mcpServers", {}))} MCP servers '
                 f'(nanobot MCP integration: configure separately in ~/.nanobot/config.json)')

    # Skills: nanobot looks for skills in a 'skills/' directory relative to cwd.
    # The bundle already unpacks skills/ so nanobot will find them automatically.
    if skills_cfg:
        log.info(f'Bundle contains skills: {skills_cfg}')

    NANOBOT_CONFIG.write_text(json.dumps(config, indent=2))
    log.info(f'~/.nanobot/config.json updated (model={model}, provider={provider})')


def _compose_prompt(bundle_path: Path, base_prompt: str, skill: str | None) -> str:
    """
    Compose the final prompt sent to nanobot.

    If a skill is specified:
      - Look for bundle_path/skills/<skill>.md
      - Strip frontmatter, prepend body to the user prompt
    """
    if not skill:
        return base_prompt

    skill_file = bundle_path / 'skills' / f'{skill}.md'
    if not skill_file.exists():
        log.warning(f'Skill file not found: {skill_file}')
        return base_prompt

    content = skill_file.read_text()
    body = re.sub(r'^---\s*\n.*?\n---\s*\n', '', content, flags=re.DOTALL).strip()
    if not body:
        return base_prompt

    return f'{body}\n\n---\n\n{base_prompt}'


def _read_frontmatter(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        content = path.read_text()
        m = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
        return yaml.safe_load(m.group(1)) or {} if m else {}
    except Exception:
        return {}


def _read_tools(bundle_path: Path) -> dict:
    for fname in ('tools.yaml', 'mcp.yaml'):
        f = bundle_path / fname
        if f.exists():
            try:
                return yaml.safe_load(f.read_text()) or {}
            except Exception:
                return {}
    return {}


def _read_skills(bundle_path: Path) -> list[str]:
    skills_dir = bundle_path / 'skills'
    if skills_dir.exists():
        return [f.stem for f in skills_dir.glob('*.md')]
    return []


def _build_env() -> dict[str, str]:
    """Environment for the nanobot subprocess. Inherits all runner env vars."""
    return dict(os.environ)