#!/usr/bin/env python3
"""Claude runner integration. Credentials never appear in command arguments or logs."""
import hashlib
import http.server
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = Path('/workspace')
CHECKPOINTS = ROOT / '.obot-checkpoints'
WORKSPACE_FILE = Path('/tmp/obot-workspace')
RESULT = Path('/tmp/obot-result.json')
MANAGED_SETTINGS = Path('/etc/claude-code/managed-settings.json')


def request(action, body, identity=False, timeout=20):
    headers = {'Content-Type': 'application/json'}
    headers['X-Obot-Runner-Token'] = os.environ['OBOT_RUNNER_TOKEN']
    url = os.environ['OBOT_URL'] + '/api/claude-runners/' + os.environ['OBOT_INSTANCE'] + '/' + action
    if identity:
        current = os.environ.get('CLAUDE_SESSION_INGRESS_TOKEN_FILE')
        if current:
            token = Path(current).read_text().strip()
        else:
            token = os.environ['CLAUDE_CODE_SESSION_ACCESS_TOKEN']
        headers['X-Claude-Session-Token'] = token
    req = urllib.request.Request(url, json.dumps(body).encode(), headers, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read()
        return json.loads(data) if data else {}


def workspace(path):
    path = Path(path).resolve()
    if (not path.is_relative_to(ROOT) or path in (ROOT, ROOT / 'repos', ROOT / 'repos/_sessions')
            or path.is_relative_to(CHECKPOINTS)):
        raise ValueError('workspace must be a directory below /workspace')
    return path


def checkpoint_path(path):
    return CHECKPOINTS / hashlib.sha256(str(path).encode()).hexdigest()


def copy_worktree(source, destination):
    # Preserve working files, including deletions, without persisting git proxy credentials.
    for item in destination.iterdir():
        if item.name == '.git':
            continue
        if item.is_symlink() or not item.is_dir():
            item.unlink()
        else:
            shutil.rmtree(item)
    shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True,
                    ignore=shutil.ignore_patterns('.git'))


def command():
    # The post-session hook has no workspace paths for zero-repository sessions.
    # Record the actual child cwd, not the runner's base directory and credentials.
    current_workspace = workspace(Path.cwd())

    manifest = CHECKPOINTS / 'workspaces.json'
    if CHECKPOINTS.is_symlink():
        raise ValueError('invalid checkpoint directory')
    if manifest.exists():
        for value in json.loads(manifest.read_text()):
            if Path(value) == ROOT / 'repos':
                # Older zero-repo checkpoints included runner internals. Recover
                # only this session's worktree, preserving the fresh config/token.
                path = current_workspace
                legacy = checkpoint_path(ROOT / 'repos')
                saved = legacy / path.relative_to(ROOT / 'repos')
                if not saved.resolve().is_relative_to(legacy):
                    raise ValueError('invalid legacy workspace checkpoint')
            else:
                path = workspace(value)
                saved = checkpoint_path(path)
            if not saved.is_dir() or saved.is_symlink():
                raise ValueError('incomplete workspace checkpoint')
            path.mkdir(parents=True, exist_ok=True)
            copy_worktree(saved, path)

    WORKSPACE_FILE.write_text(str(current_workspace))
    config = request('credentials', {}, identity=True)
    # Use the session's user scope. --mcp-config participates in the dynamic
    # server set that Claude's control plane replaces when delivering connectors.
    mcp = Path(os.environ['CLAUDE_CONFIG_DIR']) / '.claude.json'
    settings = json.loads(mcp.read_text()) if mcp.exists() else {}

    settings.setdefault('mcpServers', {}).update(config['mcpServers'])
    mcp.write_text(json.dumps(settings))
    mcp.chmod(0o600)

    # Resumed sessions may retain the old OAuth skill; authorization now uses MCP elicitation.
    (mcp.parent / 'skills/obot-vmcp-oauth/SKILL.md').unlink(missing_ok=True)

    # The runner sets ANTHROPIC_BASE_URL for each session; send Claude's API requests
    # through Obot's LLM proxy instead. Session traffic uses SESSION_INGRESS_URL.
    base_url = os.environ.pop('OBOT_ANTHROPIC_BASE_URL', '')
    if base_url:
        os.environ['ANTHROPIC_BASE_URL'] = base_url

    # exec keeps stdin and the runner's activity channel on fd 3 attached.
    binary = os.environ['CLAUDE_RUNNER_CLAUDE_BIN']
    os.execv(binary, [binary, *sys.argv[2:]])


def post_session():
    if CHECKPOINTS.is_symlink():
        raise ValueError('invalid checkpoint directory')
    CHECKPOINTS.mkdir(parents=True, exist_ok=True)
    paths = [p for p in os.environ.get('CLAUDE_RUNNER_WORKSPACE_PATHS', '').split(':') if p]
    if not paths:
        paths = [WORKSPACE_FILE.read_text()]
    for value in paths:
        path = workspace(value)
        saved = checkpoint_path(path)
        if saved.is_symlink():
            saved.unlink()
        saved.mkdir(exist_ok=True)
        copy_worktree(path, saved)
    (CHECKPOINTS / 'workspaces.json').write_text(json.dumps(paths))
    legacy = checkpoint_path(ROOT / 'repos')
    if legacy.is_symlink():
        legacy.unlink()
    elif legacy.exists():
        shutil.rmtree(legacy)

    RESULT.write_text(json.dumps({
        'checkpoint': True, 'reason': os.environ['CLAUDE_RUNNER_EXIT_REASON']}))


class SlackMetadata:
    """Best-effort display metadata from Claude Tag's transcript envelopes."""

    def __init__(self):
        self.values = {}
        self.offsets = {}

    def text(self, text):
        if text.startswith('<session-context '):
            channel = re.search(r'^Channel: #(.+?) \(id: `[^`]+`\)', text, re.MULTILINE)
            if channel:
                self.values.setdefault('slackChannelName', channel[1])

            link = re.search(r'\[Slack thread\]\((https://[a-zA-Z0-9-]+\.slack\.com/archives/[^\s)]+)\)', text)
            if link:
                self.values.setdefault('slackLink', link[1])

        if not text.startswith(('<wake ', '<wake>', '<session-context ')):
            return
        for wake in re.findall(r'<wake\b[^>]*>.*?</wake>', text, re.DOTALL):
            try:
                root = ET.fromstring(wake)
            except ET.ParseError:
                continue
            parents = {child: parent for parent in root.iter() for child in parent}
            for message in root.findall('.//message'):
                if message.get('trigger') != 'true' or message.get('from') != 'human':
                    continue
                for key, attribute in [('slackSenderName', 'author'), ('slackSenderHandle', 'author-handle')]:
                    if message.get(attribute):
                        self.values.setdefault(key, message.get(attribute))
                self.values.setdefault('slackMentioned', message.get('mention') == 'true')
                thread = message.get('thread-ts')
                parent = parents.get(message)
                if not thread and parent is not None and parent.tag == 'thread':
                    thread = parent.get('ts')
                if thread and re.fullmatch(r'[0-9]+\.[0-9]+', thread):
                    self.values.setdefault('slackThreadTimestamp', thread)

    def read(self):
        # shortcut: transcript envelopes are not a public API; replace with structured runner metadata when available.
        for path in sorted((ROOT / 'repos/_sessions').glob('*.claude-config/projects/*/*.jsonl')):
            try:
                with path.open('rb') as stream:
                    stream.seek(self.offsets.get(path, 0))
                    while True:
                        offset = stream.tell()
                        line = stream.readline()
                        if not line.endswith(b'\n'):
                            self.offsets[path] = offset
                            break
                        self.offsets[path] = stream.tell()
                        try:
                            row = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if row.get('type') != 'user':
                            continue
                        content = row.get('message', {}).get('content', [])
                        if isinstance(content, str):
                            self.text(content)
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get('type') == 'text':
                                    self.text(block.get('text', ''))
            except OSError:
                continue
        return {key: value for key, value in self.values.items()
                if isinstance(value, bool) or len(value.encode()) <= 256}


def wait_for_runner(process, stopping):
    suspend_requested = False
    metadata = SlackMetadata()
    while process.poll() is None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if stopping() or suspend_requested:
                continue
            try:
                suspend_requested = request('status', metadata.read(), timeout=5).get('suspend', False)
            except Exception:
                # A temporary Obot outage must not stop a healthy session.
                continue
            if suspend_requested and process.poll() is None:
                # Signal only the runner, which stops its child and runs the checkpoint hook.
                # Keep this supervisor alive to report finished before Substrate suspends it.
                try:
                    process.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass

    # Capture short sessions and the last transcript append before cleanup.
    try:
        values = metadata.read()
        if values:
            request('status', values, timeout=5)
    except Exception:
        pass


def configure_managed_settings():
    raw = os.environ.pop('OBOT_CLAUDE_MANAGED_SETTINGS', '')
    if not raw:
        return

    settings = json.loads(raw)
    if not isinstance(settings, dict):
        raise ValueError('managed settings must be a JSON object')

    MANAGED_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    temporary = MANAGED_SETTINGS.with_suffix('.tmp')
    temporary.write_text(json.dumps(settings))
    temporary.chmod(0o444)
    # Sessions currently run as root; file modes are not a security boundary.
    temporary.replace(MANAGED_SETTINGS)


def supervise():
    configure_managed_settings()
    ROOT.mkdir(exist_ok=True)
    result = RESULT
    result.unlink(missing_ok=True)
    WORKSPACE_FILE.unlink(missing_ok=True)

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == '/healthz' else 404)
            self.end_headers()
        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(('0.0.0.0', 80), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Substrate boots templates to produce golden snapshots. Only the intended
    # actor may consume this single-use work order; golden actors only serve health.
    if Path('/run/actor/name').read_text().strip() != os.environ['OBOT_INSTANCE']:
        while True:
            time.sleep(60)
    work_order = Path('/tmp/obot-work-order')
    work_order.write_text(os.environ.pop('OBOT_WORK_ORDER'))
    work_order.chmod(0o600)
    args = ['claude', 'self-hosted-runner', '--environment-secret-file', str(work_order),
            '--base-dir', '/workspace/repos', '--capacity', '1', '--drain-grace-sec', '0',
            '--release-idle-session-min', os.environ.get('OBOT_CLAUDE_IDLE_TIMEOUT_MINUTES', '5'),
            '--kill-session-after-min', '120',
            '--post-session-hook-timeout-sec', '300', '--hooks-dir', '/opt/obot/hooks',
            '--confine-repo-settings', 'enforce', '--use-anthropic-git-proxy', '--health-port', '8080']
    process = subprocess.Popen(args, start_new_session=True)
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    wait_for_runner(process, lambda: stopping)
    work_order.unlink(missing_ok=True)
    # Session configs/transcripts contain tokens. Only clean workspace checkpoints are durable.
    for item in ROOT.iterdir():
        if item.name == '.obot-checkpoints':
            continue
        if item.is_symlink() or not item.is_dir():
            item.unlink()
        else:
            shutil.rmtree(item)
    body = json.loads(result.read_text()) if result.exists() else {'checkpoint': False, 'reason': 'runner-exited'}
    while not stopping:
        try:
            request('finished', body)
            break
        except urllib.error.HTTPError as error:
            if error.code in (404, 409):
                break
        except Exception:
            pass
        time.sleep(5)
    # Do not restart a runner with a consumed work order. Obot snapshots this idle supervisor.
    while not stopping:
        time.sleep(5)
    server.shutdown()


if __name__ == '__main__':
    try:
        action = sys.argv[1]
        if action == 'command':
            command()
        elif action == 'post-session':
            post_session()
        elif action == 'supervise':
            supervise()
        else:
            raise ValueError('unknown runner action')
    except Exception:
        # HTTP failures and environment values may contain live credentials.
        print('Obot runner integration failed; check the environment configuration and Obot status.', file=sys.stderr)
        sys.exit(1)
