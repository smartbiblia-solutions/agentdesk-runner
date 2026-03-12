"""
agentdesk-runner v2.2  —  Execution Plane
==========================================

Architecture
------------
AgentDesk (control plane) deploys bundles and launches runs via this runner.
The runner embeds nanobot in-process, running it in **gateway mode** with a
custom channel — ``AgentDeskChannel`` — registered as a first-class
``BaseChannel`` implementation.

Why gateway mode, not CLI mode (``nanobot agent -m "..."``)?
  CLI mode calls ``AgentLoop.process_direct()`` which:
    • returns the response string directly to the caller
    • prints to terminal stdout
    • NEVER touches the MessageBus or ChannelManager
  Gateway mode drives the full MessageBus pipeline and calls
  ``channel.send(OutboundMessage)`` for every response, which is exactly
  where AgentDeskChannel intercepts and normalises events.

Event flow
----------
  POST /runs  →  _execute_run()
    1. _write_nanobot_config(bundle_path)   — merge agent.md → ~/.nanobot/config.json
    2. _compose_prompt(...)                  — prepend skill body if given
    3. Bootstrap nanobot in-process:
         MessageBus, LiteLLMProvider, SessionManager, AgentLoop, ChannelManager
    4. Register AgentDeskChannel in ChannelManager._channels dict
    5. Run AgentLoop.run() and ChannelManager.start_all() concurrently
    6. AgentDeskChannel.start() publishes InboundMessage(prompt)
    7. nanobot AgentLoop processes message, publishes OutboundMessage to bus
    8. Outbound dispatcher calls AgentDeskChannel.send(OutboundMessage)
    9. _normalise() converts to AgentDeskEvent → WS broadcast + log store

Stable event contract (AgentDeskEvent types)
--------------------------------------------
  msg:chunk    streaming text fragment (future)
  msg:done     assistant turn complete     {content}
  msg:user     user message echo           {content}
  tool:start   tool invocation begins      {tool_name, tool_input}
  tool:done    tool invocation complete    {tool_name, tool_result}
  tool:log     progress text               {text}
  run:start    run lifecycle start         {bundle}
  run:done     run lifecycle end           {exit_code}
  run:error    run error                   {message}
  run:cancelled
  sys:log      runner-internal info        {text}

API (unchanged from caller's perspective)
-----------------------------------------
  GET    /health
  POST   /bundles          multipart: bundle=<zip>, manifest=<json>
  POST   /runs             {run_id, bundle_id, prompt, skill}
  GET    /runs/{id}
  DELETE /runs/{id}
  GET    /runs/{id}/logs
  WS     /ws/runs/{id}     streaming AgentDeskEvent objects; replays on reconnect
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import uuid
import zipfile
from asyncio import Task
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
log = logging.getLogger('agentdesk-runner')

app = FastAPI(title='agentdesk-runner', version='2.2.0')
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'], allow_credentials=True,
    allow_methods=['*'], allow_headers=['*'],
)

# ── Directories ────────────────────────────────────────────────────────────────

BUNDLES_DIR = Path(os.environ.get(
    'RUNNER_BUNDLES_DIR', Path.home() / '.agentdesk-runner' / 'bundles'))
NANOBOT_CONFIG = Path(os.environ.get(
    'NANOBOT_CONFIG', Path.home() / '.nanobot' / 'config.json'))

BUNDLES_DIR.mkdir(parents=True, exist_ok=True)
NANOBOT_CONFIG.parent.mkdir(parents=True, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# AgentDeskEvent  —  stable, runner-owned event contract
# ══════════════════════════════════════════════════════════════════════════════

class AgentDeskEvent:
    MSG_CHUNK   = 'msg:chunk'
    MSG_DONE    = 'msg:done'
    MSG_USER    = 'msg:user'
    TOOL_START  = 'tool:start'
    TOOL_DONE   = 'tool:done'
    TOOL_LOG    = 'tool:log'
    RUN_START   = 'run:start'
    RUN_DONE    = 'run:done'
    RUN_ERROR   = 'run:error'
    RUN_CANCEL  = 'run:cancelled'
    SYS_LOG     = 'sys:log'

    @staticmethod
    def build(type_: str, run_id: str, **kwargs) -> dict:
        return {
            'type': type_, 'run_id': run_id,
            'ts': datetime.now(timezone.utc).isoformat(),
            **kwargs,
        }


# ── In-memory state ────────────────────────────────────────────────────────────

_bundles:     dict[str, dict]              = {}
_runs:        dict[str, dict]              = {}
_logs:        dict[str, list]              = {}
_tasks:       dict[str, Task]              = {}
_ws_channels: dict[str, list[WebSocket]]  = {}
_ad_channels: dict[str, 'AgentDeskChannel'] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# Health
# ══════════════════════════════════════════════════════════════════════════════

@app.get('/health')
def health():
    try:
        from nanobot.agent.loop import AgentLoop  # noqa: F401
        nb = 'available'
    except ImportError:
        nb = 'not installed — run: pip install nanobot-ai'
    return {'status': 'ok', 'runner_version': '2.2.0', 'nanobot': nb}


# ══════════════════════════════════════════════════════════════════════════════
# Bundles
# ══════════════════════════════════════════════════════════════════════════════

@app.post('/bundles', status_code=201)
async def receive_bundle(
    bundle:   UploadFile = File(...),
    manifest: str        = Form(...),
):
    try:
        meta = json.loads(manifest)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f'Invalid manifest JSON: {e}')

    bundle_id   = meta.get('bundle_id') or str(uuid.uuid4())
    bundle_path = BUNDLES_DIR / bundle_id

    if bundle_path.exists():
        shutil.rmtree(bundle_path)
    bundle_path.mkdir(parents=True)

    zip_bytes = await bundle.read()
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(bundle_path)
    except zipfile.BadZipFile as e:
        shutil.rmtree(bundle_path)
        raise HTTPException(400, f'Invalid zip: {e}')

    _bundles[bundle_id] = {
        'bundle_id': bundle_id,
        'path': str(bundle_path),
        'manifest': meta,
        'received_at': _now(),
    }
    log.info(f'Bundle {bundle_id} unpacked ({len(zip_bytes):,} bytes)')
    return {
        'bundle_id': bundle_id,
        'path': str(bundle_path),
        'files': [str(p.relative_to(bundle_path))
                  for p in bundle_path.rglob('*') if p.is_file()],
    }


# ══════════════════════════════════════════════════════════════════════════════
# Runs
# ══════════════════════════════════════════════════════════════════════════════

class RunCreate(BaseModel):
    run_id:    str
    bundle_id: str
    prompt:    str
    skill:     str | None = None


@app.post('/runs', status_code=201)
async def create_run(body: RunCreate):
    if body.bundle_id not in _bundles:
        raise HTTPException(404, f'Bundle {body.bundle_id!r} not found')
    if body.run_id in _runs:
        raise HTTPException(409, f'Run {body.run_id} already exists')

    bundle_path = Path(_bundles[body.bundle_id]['path'])
    ts = _now()
    _runs[body.run_id] = {
        'run_id': body.run_id, 'bundle_id': body.bundle_id,
        'prompt': body.prompt, 'skill': body.skill,
        'status': 'running', 'exit_code': None,
        'started_at': ts, 'ended_at': None,
    }
    _logs[body.run_id]        = []
    _ws_channels[body.run_id] = []

    task = asyncio.create_task(
        _execute_run(body.run_id, bundle_path, body.prompt, body.skill)
    )
    _tasks[body.run_id] = task
    return {**_runs[body.run_id], 'ws_url': f'/ws/runs/{body.run_id}'}


@app.get('/runs/{run_id}')
def get_run(run_id: str):
    if run_id not in _runs:
        raise HTTPException(404)
    return _runs[run_id]


@app.delete('/runs/{run_id}')
async def cancel_run(run_id: str):
    if run_id not in _runs:
        raise HTTPException(404)
    ch = _ad_channels.get(run_id)
    if ch:
        ch.request_cancel()
    task = _tasks.get(run_id)
    if task and not task.done():
        task.cancel()
    _runs[run_id].update({'status': 'cancelled', 'ended_at': _now()})
    await _broadcast(run_id, AgentDeskEvent.build(AgentDeskEvent.RUN_CANCEL, run_id))
    return {'ok': True}


@app.get('/runs/{run_id}/logs')
def get_run_logs(run_id: str):
    if run_id not in _runs:
        raise HTTPException(404)
    return _logs.get(run_id, [])


# ══════════════════════════════════════════════════════════════════════════════
# WebSocket
# ══════════════════════════════════════════════════════════════════════════════

@app.websocket('/ws/runs/{run_id}')
async def ws_run(run_id: str, websocket: WebSocket):
    await websocket.accept()
    _ws_channels.setdefault(run_id, []).append(websocket)

    for entry in _logs.get(run_id, []):
        try:
            await websocket.send_text(json.dumps(entry))
        except Exception:
            break

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if msg.get('type') == 'ping':
                await websocket.send_text(json.dumps({'type': 'pong'}))
            elif msg.get('type') == 'msg:user':
                ch = _ad_channels.get(run_id)
                if ch:
                    await ch.receive_inbound(msg.get('content', ''))
    except WebSocketDisconnect:
        pass
    finally:
        if run_id in _ws_channels:
            _ws_channels[run_id] = [
                w for w in _ws_channels[run_id] if w is not websocket]


# ══════════════════════════════════════════════════════════════════════════════
# AgentDeskChannel  —  proper nanobot BaseChannel implementation
#
# Nanobot's ChannelManager calls:
#   channel.start()              — runs the channel's event loop
#   channel.stop()               — shuts it down
#   channel.send(OutboundMessage)— called by outbound dispatcher per response
#   channel.is_allowed(sender)   — ACL gate
#
# We register this as the sole channel; nanobot's gateway drives the message
# bus, and all output lands in send() where we normalise → AgentDeskEvent.
# ══════════════════════════════════════════════════════════════════════════════

CHANNEL_NAME = 'agentdesk'


class AgentDeskChannel:
    """
    First-class nanobot channel that bridges nanobot's MessageBus to
    AgentDesk's WebSocket event stream.

    Lifecycle (per run):
      1. Constructed with run_id + prompt + bus reference.
      2. ChannelManager calls start() — publishes InboundMessage(prompt),
         then waits for _stop_event or additional inbound messages.
      3. AgentLoop processes message; publishes OutboundMessage to bus.
      4. ChannelManager's outbound dispatcher calls send(OutboundMessage).
      5. send() normalises → list[AgentDeskEvent] → broadcast to WS.
      6. On run completion: _execute_run calls channel.stop() → _stop_event.set()
         → start() returns → ChannelManager.start_all() task ends.
    """

    name = CHANNEL_NAME   # ChannelManager uses this as the dispatch key

    def __init__(self, run_id: str, prompt: str, bus) -> None:
        self.run_id  = run_id
        self.prompt  = prompt
        self.bus     = bus
        self._stop_event:     asyncio.Event         = asyncio.Event()
        self._inbound_queue:  asyncio.Queue[str]    = asyncio.Queue()

    # ── nanobot channel contract ──────────────────────────────────────────────

    def is_allowed(self, sender_id: str) -> bool:
        return True

    async def start(self) -> None:
        """
        Publish the run's initial prompt, then keep the channel alive
        (forwarding human-in-the-loop messages) until _stop_event fires.
        """
        from nanobot.bus.events import InboundMessage

        initial = InboundMessage(
            channel   = CHANNEL_NAME,
            chat_id   = self.run_id,
            sender_id = self.run_id,
            content   = self.prompt,
        )
        await self.bus.publish_inbound(initial)
        await _emit(self.run_id, AgentDeskEvent.build(
            AgentDeskEvent.MSG_USER, self.run_id, content=self.prompt))

        # Keep alive; forward human-in-the-loop messages
        while not self._stop_event.is_set():
            try:
                content = await asyncio.wait_for(
                    self._inbound_queue.get(), timeout=1.0)
                msg = InboundMessage(
                    channel   = CHANNEL_NAME,
                    chat_id   = self.run_id,
                    sender_id = self.run_id,
                    content   = content,
                )
                await self.bus.publish_inbound(msg)
                await _emit(self.run_id, AgentDeskEvent.build(
                    AgentDeskEvent.MSG_USER, self.run_id, content=content))
            except asyncio.TimeoutError:
                continue

    async def stop(self) -> None:
        self._stop_event.set()

    async def send(self, msg) -> None:
        """
        Called by nanobot's outbound dispatcher for every OutboundMessage
        whose channel == CHANNEL_NAME or whose chat_id == run_id.

        msg (nanobot OutboundMessage Pydantic model):
          .content   str  — agent response text
          .channel   str  — 'agentdesk'
          .chat_id   str  — run_id
          .metadata  dict — optional tool/progress fields
        """
        for evt in _normalise(self.run_id, msg):
            await _emit(self.run_id, evt)

    # ── AgentDesk-specific ────────────────────────────────────────────────────

    async def receive_inbound(self, content: str) -> None:
        """Push a user message from the WS into the running session."""
        await self._inbound_queue.put(content)

    def request_cancel(self) -> None:
        """Signal the channel loop to stop (called from DELETE /runs/{id})."""
        self._stop_event.set()


# ══════════════════════════════════════════════════════════════════════════════
# Normalisation  —  nanobot OutboundMessage → AgentDeskEvent list
# ══════════════════════════════════════════════════════════════════════════════

def _normalise(run_id: str, msg) -> list[dict]:
    """
    Convert a nanobot OutboundMessage to a list of AgentDeskEvents.

    The frontend NEVER sees nanobot field names — only AgentDeskEvent types.
    If nanobot's internal model changes, only this function needs updating.

    OutboundMessage.metadata may contain:
      'progress'    str  — tool progress text
      'tool_name'   str  — name of executing tool
      'tool_input'  any  — tool call arguments
      'tool_result' any  — tool call output (present when tool finishes)
    """
    meta    = getattr(msg, 'metadata', {}) or {}
    content = getattr(msg, 'content',  '') or ''
    events: list[dict] = []

    if meta.get('progress'):
        events.append(AgentDeskEvent.build(
            AgentDeskEvent.TOOL_LOG, run_id, text=meta['progress']))

    if meta.get('tool_name') and 'tool_result' not in meta:
        events.append(AgentDeskEvent.build(
            AgentDeskEvent.TOOL_START, run_id,
            tool_name  = meta['tool_name'],
            tool_input = meta.get('tool_input'),
        ))

    if meta.get('tool_name') and 'tool_result' in meta:
        events.append(AgentDeskEvent.build(
            AgentDeskEvent.TOOL_DONE, run_id,
            tool_name   = meta['tool_name'],
            tool_result = _truncate(str(meta.get('tool_result', ''))),
        ))

    if content:
        events.append(AgentDeskEvent.build(
            AgentDeskEvent.MSG_DONE, run_id, content=content))

    return events


def _truncate(s: str, max_len: int = 500) -> str:
    return s if len(s) <= max_len else s[:max_len] + '…'


# ══════════════════════════════════════════════════════════════════════════════
# Execution engine  —  nanobot in gateway mode, in-process
# ══════════════════════════════════════════════════════════════════════════════

async def _execute_run(
    run_id:      str,
    bundle_path: Path,
    prompt:      str,
    skill:       str | None,
) -> None:
    """
    Bootstrap nanobot gateway in-process with AgentDeskChannel as sole channel.

    Steps:
      1. Write ~/.nanobot/config.json from bundle's agent.md frontmatter
      2. Compose final prompt (prepend skill markdown body if skill given)
      3. Bootstrap: MessageBus + LiteLLMProvider + SessionManager + AgentLoop
      4. Build ChannelManager; inject AgentDeskChannel as its only channel
      5. Run AgentLoop.run() + ChannelManager.start_all() concurrently
         — AgentDeskChannel.start() publishes InboundMessage(prompt), waits
         — AgentLoop processes message, publishes OutboundMessage to bus
         — Dispatcher calls AgentDeskChannel.send() → _normalise() → broadcast
      6. Once response detected, teardown and emit run:done / run:error
    """
    await _emit(run_id, AgentDeskEvent.build(
        AgentDeskEvent.RUN_START, run_id, bundle=bundle_path.name))

    try:
        # 1. Config
        _write_nanobot_config(bundle_path)
        await _sys_log(run_id, 'nanobot config written')

        # 2. Prompt
        final_prompt = _compose_prompt(bundle_path, prompt, skill)
        await _sys_log(run_id, f'skill={skill or "none"} | prompt: {final_prompt[:120]}')

        # 3. Bootstrap nanobot
        from nanobot.config.loader              import load_config
        from nanobot.bus.queue                  import MessageBus
        from nanobot.agent.loop                 import AgentLoop
        from nanobot.session.manager            import SessionManager
        from nanobot.channels.manager           import ChannelManager

        workspace = bundle_path / 'workspace'
        if not workspace.exists():
            workspace = bundle_path

        nb_config = load_config()
        bus       = MessageBus()

        # Use nanobot's own _make_provider() — it resolves api_key, api_base,
        # and provider_name from the loaded config exactly as the gateway does.
        # LiteLLMProvider.__init__ takes (api_key, api_base, default_model, ...)
        # as individual strings — NEVER the ProvidersConfig object.
        try:
            from nanobot.cli.commands import _make_provider
            provider = _make_provider(nb_config)
        except (ImportError, AttributeError):
            # Fallback: construct with no args — nanobot will read credentials
            # from ~/.nanobot/config.json which we already wrote above.
            from nanobot.providers.litellm_provider import LiteLLMProvider
            provider = LiteLLMProvider()

        session_mgr = SessionManager(workspace)

        defaults   = (nb_config.agents.defaults
                      if nb_config.agents and nb_config.agents.defaults else None)
        agent_loop = AgentLoop(
            bus             = bus,
            provider        = provider,
            workspace       = workspace,
            model           = defaults.model       if defaults else None,
            temperature     = defaults.temperature if defaults else 0.3,
            max_tokens      = defaults.max_tokens  if defaults else 4096,
            session_manager = session_mgr,
        )

        # 4. Register AgentDeskChannel
        channel = AgentDeskChannel(run_id, final_prompt, bus)
        _ad_channels[run_id] = channel

        # ChannelManager.__init__ may try to load channels from config.
        # We override _channels after construction to ensure only ours runs.
        chan_mgr = ChannelManager(bus=bus, config=nb_config)
        chan_mgr._channels = {CHANNEL_NAME: channel}

        await _sys_log(run_id, 'nanobot gateway started')

        # 5. Run concurrently; wait for response + teardown
        agent_task = asyncio.create_task(agent_loop.run())
        chan_task  = asyncio.create_task(chan_mgr.start_all())

        await _wait_for_response(run_id, channel, agent_task, chan_task)

    except asyncio.CancelledError:
        _runs[run_id].update({'status': 'cancelled', 'ended_at': _now()})
        await _broadcast(run_id, AgentDeskEvent.build(AgentDeskEvent.RUN_CANCEL, run_id))
        return

    except ImportError as e:
        msg = f'nanobot not installed: {e}  — run: pip install nanobot-ai'
        log.error(f'[run:{run_id}] {msg}')
        await _sys_log(run_id, msg)
        _runs[run_id].update({'status': 'error', 'exit_code': 127, 'ended_at': _now()})
        await _broadcast(run_id, AgentDeskEvent.build(
            AgentDeskEvent.RUN_ERROR, run_id, message=msg))
        return

    except Exception as e:
        log.exception(f'[run:{run_id}] error: {e}')
        await _sys_log(run_id, f'error: {e}')
        _runs[run_id].update({'status': 'error', 'exit_code': 1, 'ended_at': _now()})
        await _broadcast(run_id, AgentDeskEvent.build(
            AgentDeskEvent.RUN_ERROR, run_id, message=str(e)))
        return

    finally:
        _ad_channels.pop(run_id, None)
        _tasks.pop(run_id, None)

    _runs[run_id].update({'status': 'done', 'exit_code': 0, 'ended_at': _now()})
    await _broadcast(run_id, AgentDeskEvent.build(
        AgentDeskEvent.RUN_DONE, run_id, exit_code=0))
    log.info(f'[run:{run_id}] done')


async def _wait_for_response(
    run_id:     str,
    channel:    AgentDeskChannel,
    agent_task: asyncio.Task,
    chan_task:  asyncio.Task,
    timeout:    float = 600.0,
) -> None:
    """
    Wait until the agent delivers at least one msg:done event, then tear down.

    - Polls every 0.5s for up to ``timeout`` seconds (default 10 min).
    - Once a msg:done/msg:chunk is seen, waits an additional drain window
      (2s) to let any trailing events arrive before shutting down.
    - Respects external cancel (channel._stop_event already set).
    - Propagates any exception from agent_task.
    """
    poll = 0.5
    elapsed = 0.0
    got_response = False

    while elapsed < timeout:
        await asyncio.sleep(poll)
        elapsed += poll

        # External cancel (DELETE /runs or WS cancel message)
        if channel._stop_event.is_set():
            break

        # Crash in agent
        if agent_task.done() and not agent_task.cancelled():
            exc = agent_task.exception()
            if exc:
                raise exc

        # Check for first response
        logs = _logs.get(run_id, [])
        has_response = any(
            e.get('type') in (AgentDeskEvent.MSG_DONE, AgentDeskEvent.MSG_CHUNK)
            for e in logs
        )

        if has_response and not got_response:
            got_response = True
            # Drain window: wait 2s for any trailing events from same turn
            count_before = len(logs)
            await asyncio.sleep(2.0)
            count_after = len(_logs.get(run_id, []))
            if count_after == count_before:
                break   # quiet — run is complete

    # Teardown
    await channel.stop()
    agent_task.cancel()
    chan_task.cancel()
    await asyncio.gather(agent_task, chan_task, return_exceptions=True)

    if not got_response and not channel._stop_event.is_set():
        raise RuntimeError(f'Run timed out after {timeout:.0f}s without a response')


# ══════════════════════════════════════════════════════════════════════════════
# Broadcast / emit helpers
# ══════════════════════════════════════════════════════════════════════════════

async def _emit(run_id: str, evt: dict) -> None:
    _logs.setdefault(run_id, []).append(evt)
    await _broadcast(run_id, evt)


async def _broadcast(run_id: str, data: dict) -> None:
    dead = []
    for ws in _ws_channels.get(run_id, []):
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            dead.append(ws)
    for ws in dead:
        if run_id in _ws_channels:
            _ws_channels[run_id] = [
                w for w in _ws_channels[run_id] if w is not ws]


async def _sys_log(run_id: str, text: str) -> None:
    log.info(f'[run:{run_id}] {text}')
    await _emit(run_id, AgentDeskEvent.build(
        AgentDeskEvent.SYS_LOG, run_id, text=text))


# ══════════════════════════════════════════════════════════════════════════════
# Nanobot config translation
# ══════════════════════════════════════════════════════════════════════════════

def _write_nanobot_config(bundle_path: Path) -> None:
    """
    Write ONLY the agent runtime parameters (model, temperature, maxTokens)
    into ~/.nanobot/config.json under agents.defaults.

    Design contract — logic-only sync:
      AgentDesk owns:     model, temperature, max_tokens  (from agent.md frontmatter)
      Runner admin owns:  providers block (API keys, base_urls, etc.)

    The providers block is NEVER modified here — it survives every run intact.
    This means:
      • API keys stay on the runner, set up once manually by the runner admin
      • The AgentDesk chat model and the agent execution model are independent
      • No credentials ever travel through bundles

    A deep-copy of the existing config is used so ALL nested structures
    (providers, channels, cron, etc.) are fully preserved.
    """
    import copy

    meta = _read_frontmatter(bundle_path / 'agent.md')

    # Deep-copy entire existing config — providers, channels, etc. untouched
    existing: dict = {}
    if NANOBOT_CONFIG.exists():
        try:
            existing = json.loads(NANOBOT_CONFIG.read_text()) or {}
        except json.JSONDecodeError:
            log.warning('Existing config.json invalid — starting from empty config')

    config = copy.deepcopy(existing)

    # Write ONLY agents.defaults — never touch providers block
    defaults = config.setdefault('agents', {}).setdefault('defaults', {})

    model = meta.get('model')
    if model:
        defaults['model'] = model

    temp = meta.get('temperature')
    if temp is not None:
        defaults['temperature'] = temp

    max_tok = meta.get('max_tokens')
    if max_tok is not None:
        defaults['maxTokens'] = max_tok

    NANOBOT_CONFIG.write_text(json.dumps(config, indent=2))
    log.info(
        f'config.json updated — agents.defaults: '
        f'model={model} temperature={temp} maxTokens={max_tok} '
        f'| providers block: untouched ({len(config.get("providers", {}))} entries)'
    )


def _compose_prompt(bundle_path: Path, base_prompt: str, skill: str | None) -> str:
    if not skill:
        return base_prompt
    skill_file = bundle_path / 'skills' / f'{skill}.md'
    if not skill_file.exists():
        log.warning(f'Skill {skill!r} not found in bundle')
        return base_prompt
    content = skill_file.read_text()
    body = re.sub(r'^---\s*\n.*?\n---\s*\n', '', content, flags=re.DOTALL).strip()
    return f'{body}\n\n---\n\n{base_prompt}' if body else base_prompt


def _read_frontmatter(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        content = path.read_text()
        m = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
        return yaml.safe_load(m.group(1)) or {} if m else {}
    except Exception:
        return {}
