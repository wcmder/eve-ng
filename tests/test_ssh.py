import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import paramiko

from eve_lab.config import load_server
from eve_lab.dhcp import clear


class ConfigTests(unittest.TestCase):
    def test_separate_credentials_reload_and_environment_override(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            root = Path(directory)
            (root / 'config').mkdir()
            (root / 'config/servers.yaml').write_text('servers:\n  default:\n    url: http://example.test\n')
            env = root / '.env'
            env.write_text('EVE_SSH_USERNAME=root\nEVE_SSH_PASSWORD=first\n')
            self.assertEqual(load_server(root, 'default', 'ssh')['ssh_password'], 'first')
            with self.assertRaisesRegex(ValueError, 'EVE_USERNAME'):
                load_server(root, 'default')
            env.write_text('EVE_SSH_USERNAME=root\nEVE_SSH_PASSWORD=changed\n')
            self.assertEqual(load_server(root, 'default', 'ssh')['ssh_password'], 'changed')
            with patch.dict('os.environ', {'EVE_SSH_PASSWORD': 'override'}):
                self.assertEqual(load_server(root, 'default', 'ssh')['ssh_password'], 'override')
            env.write_text('EVE_USERNAME=admin\nEVE_PASSWORD=web\n')
            self.assertEqual(load_server(root, 'default')['password'], 'web')
            with self.assertRaisesRegex(ValueError, 'EVE_SSH_USERNAME'):
                load_server(root, 'default', 'ssh')


class SshTests(unittest.TestCase):
    server = {'url': 'http://example.test', 'ssh_username': 'root', 'ssh_password': 'secret-test'}

    @patch('eve_lab.dhcp.paramiko.SSHClient')
    def test_password_transport_and_dry_run(self, factory):
        client = factory.return_value
        stdout = MagicMock()
        stdout.read.return_value = b'{"dry_run": true}'
        stdout.channel.recv_exit_status.return_value = 0
        client.exec_command.return_value = (MagicMock(), stdout, io.BytesIO(b''))
        self.assertEqual(clear(self.server, 'pnet1', True), {'dry_run': True})
        client.load_system_host_keys.assert_called_once()
        client.connect.assert_called_once_with(hostname='example.test', username='root',
            password='secret-test', timeout=10, auth_timeout=10, banner_timeout=10,
            allow_agent=False, look_for_keys=False)
        command = client.exec_command.call_args.args[0]
        self.assertTrue(command.endswith(' --dry-run'))
        self.assertNotIn('secret-test', command)
        client.close.assert_called_once()

    @patch('eve_lab.dhcp.paramiko.SSHClient')
    def test_auth_failure_closes_connection_without_password(self, factory):
        factory.return_value.connect.side_effect = paramiko.AuthenticationException('secret-test')
        with self.assertRaisesRegex(RuntimeError, 'SSH authentication failed') as error:
            clear(self.server, 'pnet1')
        self.assertNotIn('secret-test', str(error.exception))
        factory.return_value.close.assert_called_once()
