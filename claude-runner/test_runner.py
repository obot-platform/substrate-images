import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import runner


class ManagedSettingsTest(unittest.TestCase):
    def test_settings_are_written_and_removed_from_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'etc/managed-settings.json'
            with patch.object(runner, 'MANAGED_SETTINGS', destination), patch.dict(os.environ, {'OBOT_CLAUDE_MANAGED_SETTINGS': '{"permissions":{"deny":["Read(./secret)"]}}'}):
                runner.configure_managed_settings()
                self.assertEqual(json.loads(destination.read_text()), {'permissions': {'deny': ['Read(./secret)']}})
                self.assertEqual(destination.stat().st_mode & 0o777, 0o444)
                self.assertNotIn('OBOT_CLAUDE_MANAGED_SETTINGS', os.environ)

                os.environ['OBOT_CLAUDE_MANAGED_SETTINGS'] = '{}'
                runner.configure_managed_settings()
                self.assertEqual(json.loads(destination.read_text()), {})

    def test_blank_preserves_image_default_and_invalid_input_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'managed-settings.json'
            destination.write_text('{"model":"opus"}')
            with patch.object(runner, 'MANAGED_SETTINGS', destination), patch.dict(os.environ, {'OBOT_CLAUDE_MANAGED_SETTINGS': ''}):
                runner.configure_managed_settings()
                self.assertEqual(json.loads(destination.read_text()), {'model': 'opus'})
                for value in ['[]', 'null', '"text"', '{']:
                    os.environ['OBOT_CLAUDE_MANAGED_SETTINGS'] = value
                    with self.assertRaises(ValueError):
                        runner.configure_managed_settings()


class CheckpointTest(unittest.TestCase):
    def test_resume_preserves_work_and_fresh_runner_credentials(self):
        for kind in ('zero-repo', 'legacy', 'repository'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve() / 'workspace'
                repos = root / 'repos'
                work = repos / ('project' if kind == 'repository' else '_sessions/cse_test')
                config = repos / '_sessions/cse_test.claude-config'
                work.mkdir(parents=True)
                config.mkdir(parents=True)
                token = config / '.session_ingress_token.e1'
                token.write_text('old token')
                (config / '.claude.json').write_text('{"old":true}')
                (work / 'notes.txt').write_text('user work')
                (work / '.git').mkdir()
                (work / '.git/credentials').write_text('old git credentials')

                with (patch.object(runner, 'ROOT', root),
                      patch.object(runner, 'CHECKPOINTS', root / '.obot-checkpoints'),
                      patch.object(runner, 'WORKSPACE_FILE', Path(directory) / 'cwd'),
                      patch.object(runner, 'RESULT', Path(directory) / 'result'),
                      patch.object(Path, 'cwd', return_value=work),
                      patch.dict(os.environ, {
                          'CLAUDE_CONFIG_DIR': str(config),
                          'CLAUDE_RUNNER_CLAUDE_BIN': '/claude',
                          'CLAUDE_RUNNER_WORKSPACE_PATHS': str(work) if kind == 'repository' else '',
                          'CLAUDE_RUNNER_EXIT_REASON': 'completed',
                      }),
                      patch.object(runner, 'request', return_value={'mcpServers': {}}) as request,
                      patch.object(os, 'execv') as execute):
                    runner.command()
                    if kind == 'legacy':
                        # Reproduce the old checkpoint that persisted the entire base dir.
                        runner.CHECKPOINTS.mkdir()
                        shutil.copytree(repos, runner.checkpoint_path(repos))
                        (runner.CHECKPOINTS / 'workspaces.json').write_text(json.dumps([str(repos)]))
                    else:
                        runner.post_session()

                    shutil.rmtree(repos)
                    work.mkdir(parents=True)
                    config.mkdir(parents=True)
                    token.write_text('fresh token')
                    (config / '.claude.json').write_text('{"fresh":true}')
                    (work / 'deleted.txt').write_text('remove on restore')
                    (work / '.git').mkdir()
                    (work / '.git/credentials').write_text('fresh git credentials')
                    inode = work.stat().st_ino

                    runner.command()

                    self.assertEqual(work.stat().st_ino, inode)
                    self.assertEqual((work / 'notes.txt').read_text(), 'user work')
                    self.assertFalse((work / 'deleted.txt').exists())
                    self.assertEqual(token.read_text(), 'fresh token')
                    self.assertEqual(json.loads((config / '.claude.json').read_text()),
                                     {'fresh': True, 'mcpServers': {}})
                    self.assertEqual((work / '.git/credentials').read_text(), 'fresh git credentials')
                    self.assertEqual(execute.call_count, 2)
                    request.assert_called_with('credentials', {}, identity=True)

                    runner.post_session()

                    self.assertEqual(json.loads((runner.CHECKPOINTS / 'workspaces.json').read_text()),
                                     [str(work)])
                    self.assertFalse(runner.checkpoint_path(repos).exists())
                    self.assertEqual([p.name for p in runner.checkpoint_path(work).iterdir()], ['notes.txt'])

    def test_runner_base_directories_are_not_workspaces(self):
        for path in (runner.ROOT, runner.ROOT / 'repos', runner.ROOT / 'repos/_sessions'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                runner.workspace(path)


class MCPConfigTest(unittest.TestCase):
    def test_command_installs_only_egress_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            work = root / 'project'
            config = root / 'config'
            work.mkdir()
            config.mkdir()
            servers = {'vmcp_one': {'type': 'http', 'url': 'http://obot/mcp-connect/vmcp_one',
                                   'headers': {'X-Obot-Egress-Key': 'placeholder'}}}
            with (patch.object(runner, 'ROOT', root),
                  patch.object(runner, 'CHECKPOINTS', root / 'checkpoints'),
                  patch.object(runner, 'WORKSPACE_FILE', root / 'current-workspace'),
                  patch.object(Path, 'cwd', return_value=work),
                  patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(config), 'CLAUDE_RUNNER_CLAUDE_BIN': '/bin/true'}),
                  patch.object(runner, 'request', return_value={'mcpServers': servers}),
                  patch.object(os, 'execv')):
                runner.command()
                skill = config / 'skills/obot-vmcp-oauth/SKILL.md'
                self.assertFalse(skill.exists())
                skill.parent.mkdir(parents=True)
                skill.write_text('stale skill from previous session')
                runner.command()
                self.assertFalse(skill.exists())

                # A resumed session with no MCP grants must lose the injected skill.
                with patch.object(runner, 'request', return_value={'mcpServers': {}}):
                    runner.command()
                self.assertFalse(skill.exists())
            saved = json.loads((config / '.claude.json').read_text())
            self.assertEqual(saved['mcpServers'], servers)
            self.assertNotIn('headersHelper', saved['mcpServers']['vmcp_one'])
            self.assertNotIn('Authorization', saved['mcpServers']['vmcp_one']['headers'])


class SuspendTest(unittest.TestCase):
    def test_suspension_drains_runner_once_after_transient_error(self):
        process = Mock()
        process.poll.side_effect = [None, None, None, None, 0]
        process.wait.side_effect = [subprocess.TimeoutExpired('claude', 5)] * 3
        with patch.object(runner, 'request', side_effect=[OSError(), {'suspend': True}]) as request:
            runner.wait_for_runner(process, lambda: False)
        self.assertEqual(request.call_count, 2)
        process.send_signal.assert_called_once_with(signal.SIGTERM)
        process.kill.assert_not_called()

    def test_finished_runner_needs_no_suspend_request(self):
        process = Mock()
        process.poll.return_value = 0
        with patch.object(runner, 'request') as request:
            runner.wait_for_runner(process, lambda: False)
        request.assert_not_called()
        process.send_signal.assert_not_called()


class SlackMetadataTest(unittest.TestCase):
    def test_first_values_and_false_are_preserved(self):
        metadata = runner.SlackMetadata()
        metadata.text('<session-context nonce="x">\nChannel: #craig-test (id: `C123`)\n</session-context>')
        metadata.text('<wake reason="channel-activity"><channel><message trigger="true" from="human" author="Donnie &amp; friends" author-handle="thedadams">hello</message></channel></wake>')
        metadata.text('<wake><channel><message trigger="true" from="human" author="Other" author-handle="other" mention="true">hello</message></channel></wake>')
        self.assertEqual(metadata.values, {
            'slackChannelName': 'craig-test', 'slackSenderName': 'Donnie & friends',
            'slackSenderHandle': 'thedadams', 'slackMentioned': False})

    def test_thread_metadata_and_combined_context(self):
        metadata = runner.SlackMetadata()
        link = 'https://acorn-io.slack.com/archives/C123/p123456?thread_ts=123.456&cid=C123'
        metadata.text('<session-context nonce="x">\n[Slack thread](' + link + ')\n</session-context>\n'
                      '<wake><channel><message trigger="true" from="human" thread-ts="123.456">hi</message></channel></wake>')
        self.assertEqual(metadata.values['slackThreadTimestamp'], '123.456')
        self.assertEqual(metadata.values['slackLink'], link)
        metadata.text('<wake><channel><thread ts="789.123"><message trigger="true" from="human">hi</message></thread></channel></wake>')
        metadata.text('<session-context nonce="y">[Slack thread](https://other.slack.com/archives/C456/p789)</session-context>')
        self.assertEqual(metadata.values['slackThreadTimestamp'], '123.456')
        self.assertEqual(metadata.values['slackLink'], link)

        nested = runner.SlackMetadata()
        nested.text('<wake><channel><thread ts="789.123"><message trigger="true" from="human">hi</message></thread></channel></wake>')
        self.assertEqual(nested.values['slackThreadTimestamp'], '789.123')

    def test_incremental_transcript_and_partial_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'repos/_sessions/test.claude-config/projects/project/session.jsonl'
            path.parent.mkdir(parents=True)
            row = json.dumps({'type': 'user', 'message': {'content': [
                {'type': 'text', 'text': '<wake><message trigger="true" from="human" author="Donnie" mention="true">hi</message></wake>'}]}})
            path.write_text(row[:30])
            metadata = runner.SlackMetadata()
            with patch.object(runner, 'ROOT', root):
                self.assertEqual(metadata.read(), {})
                with path.open('a') as stream:
                    stream.write(row[30:] + '\n')
                self.assertEqual(metadata.read(), {'slackSenderName': 'Donnie', 'slackMentioned': True})
                self.assertEqual(metadata.read(), metadata.values)

    def test_ignores_tool_output_bots_and_malformed_envelopes(self):
        metadata = runner.SlackMetadata()
        for text in ['ordinary text <wake>fake</wake>', '<wake malformed',
                     '<wake><message trigger="true" from="agent" author="Bot"/></wake>']:
            metadata.text(text)
        self.assertEqual(metadata.values, {})


if __name__ == '__main__':
    unittest.main()
