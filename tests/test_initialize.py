import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from eve_lab.initialize import initialize, PaloConsole, config_commands
from eve_lab.client import EveClient


class InitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / 'labs/test/configs'
        self.base.mkdir(parents=True)
        (self.base / 'R0-init.cfg').write_text('hostname R0\n')
        self.nodes = {'7': {'name': 'R0', 'template': 'c8000v', 'status': 2,
                            'console': 'telnet', 'url': 'telnet://10.0.4.4:32775'}}
        self.client = MagicMock()
        self.client.request.return_value = self.nodes

    def run_init(self, **kwargs):
        return initialize(self.client, {'name': 'test'}, self.root, 'default', **kwargs)

    @patch('eve_lab.initialize.paramiko.SSHClient')
    def test_check_discovers_port_without_ssh(self, ssh):
        result = self.run_init(check=True)
        self.assertEqual(result['planned'][0]['port'], 32775)
        ssh.assert_not_called()

    def test_missing_and_unsupported_skipped(self):
        self.nodes['8'] = {'name': 'Other', 'template': 'unsupported'}
        (self.base / 'R0-init.cfg').unlink()
        self.assertEqual(len(self.run_init(check=True)['skipped']), 2)

    def test_stopped_and_non_telnet_rejected(self):
        self.nodes['7']['status'] = 0
        with self.assertRaisesRegex(ValueError, 'Start R0'):
            self.run_init(check=True)
        self.nodes['7']['status'] = 2
        self.nodes['7']['url'] = 'vnc://10.0.4.4:5900'
        with self.assertRaisesRegex(ValueError, 'Telnet'):
            self.run_init(check=True)

    def test_vnc_console_is_skipped_without_blocking_router(self):
        self.nodes['8'] = {'name': 'PA', 'template': 'paloalto', 'console': 'vnc'}
        result = self.run_init(check=True)
        self.assertEqual(result['planned'][0]['node'], 'R0')
        self.assertIn('Telnet serial console', result['skipped'][0]['reason'])

    def test_login_can_explicitly_request_native_console_urls(self):
        client = EveClient('http://example.invalid')
        client.request = MagicMock()
        client.login('admin', 'test', html5=False)
        client.request.assert_called_once_with('POST', 'auth/login',
                                               {'username': 'admin', 'password': 'test', 'html5': '0'})
    @patch('eve_lab.initialize.Console')
    @patch('eve_lab.initialize.paramiko.SSHClient')
    @patch('eve_lab.initialize.credentials', return_value=['admin', 'testpass', 'testenable'])
    @patch('eve_lab.initialize.load_server', return_value={'url': 'http://10.0.4.4', 'ssh_username': 'root', 'ssh_password': 'test'})
    def test_execution_uses_api_port_and_credentials(self, server, creds, ssh, console):
        result = self.run_init(timeout=900)
        channel = ssh.return_value.get_transport.return_value.open_session.return_value
        channel.exec_command.assert_called_once_with('telnet 127.0.0.1 32775')
        console.assert_called_once_with(channel, boot_timeout=900)
        console.return_value.initialize.assert_called_once_with(['hostname R0'], username='admin', password='testpass')
        self.assertEqual(result['completed'], ['R0'])
        channel.close.assert_called_once()

    def test_palo_accepts_only_config_commands(self):
        path = self.base / 'PA-init.cfg'
        path.write_text('set deviceconfig system hostname PA\ncommit\n')
        with self.assertRaises(ValueError): config_commands(path, 'paloalto')

    def test_palo_does_not_claim_failed_commit_success(self):
        c = PaloConsole(MagicMock(), boot_timeout=600)
        c.command = MagicMock(return_value='Commit job queued')
        with self.assertRaisesRegex(RuntimeError, 'commit not confirmed'):
            c.initialize(['set deviceconfig system hostname PA'])
        c.command = MagicMock(return_value='Configuration committed successfully')
        c.initialize(['set deviceconfig system hostname PA'])
        self.assertEqual(c.command.call_args.args, ('exit',))

    def test_palo_mandatory_password_change_is_explicit(self):
        c = PaloConsole(MagicMock(), boot_timeout=600)
        match = MagicMock()
        match.group.return_value = 'Enter new password :'
        c.expect = MagicMock(return_value=('', match))
        with self.assertRaisesRegex(RuntimeError, 'mandatory password change'):
            c.login('admin', 'test')
