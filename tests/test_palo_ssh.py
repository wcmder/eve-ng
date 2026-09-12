from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import paramiko

from eve_lab.palo_ssh import management_targets, connect_palo
from eve_lab.initialize import initialize


class PaloSSHTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lab = self.root / 'labs/test'
        (self.lab / 'configs').mkdir(parents=True)
        (self.lab / 'configs/PA-init.cfg').write_text('set deviceconfig system hostname PA\n')
        self.client = MagicMock()
        self.client.request.return_value = {'6': {'name': 'PA', 'template': 'paloalto', 'console': 'vnc', 'status': 2}}

    def test_targets_are_per_node_and_override_is_validated(self):
        (self.lab / 'init.yaml').write_text('PA:\n  management_ip: 172.16.1.134\n')
        self.assertEqual(management_targets(self.root, 'test')['PA'], '172.16.1.134')
        self.assertEqual(management_targets(self.root, 'test', 'PA', '172.16.1.135')['PA'], '172.16.1.135')
        with self.assertRaises(ValueError): management_targets(self.root, 'test', management_ip='172.16.1.134')
        with self.assertRaises(ValueError): management_targets(self.root, 'test', 'PA', 'bad;command')

    @patch('eve_lab.initialize.paramiko.SSHClient')
    def test_vnc_node_preview_uses_management_ssh_without_connecting(self, ssh):
        result = initialize(self.client, {'name': 'test'}, self.root, 'default',
                            node_name='PA', management_ip='172.16.1.134', check=True)
        self.assertEqual(result['planned'][0]['transport'], 'ssh')
        self.assertEqual(result['planned'][0]['port'], 22)
        self.assertEqual(result['skipped'], [])
        ssh.assert_not_called()

    @patch('eve_lab.palo_ssh.paramiko.SSHClient')
    def test_connect_uses_tunnel_and_device_credentials(self, factory):
        host = MagicMock()
        device, channel = connect_palo(host, '172.16.1.134', 'admin', 'testpass', 60)
        self.assertEqual(host.get_transport.return_value.open_channel.call_args.args,
                         ('direct-tcpip', ('172.16.1.134', 22), ('127.0.0.1', 0)))
        kwargs = device.connect.call_args.kwargs
        self.assertEqual(kwargs['username'], 'admin')
        self.assertEqual(kwargs['password'], 'testpass')
        self.assertIs(kwargs['sock'], host.get_transport.return_value.open_channel.return_value)
        device.set_missing_host_key_policy.assert_not_called()

    @patch('eve_lab.palo_ssh.paramiko.SSHClient')
    def test_authentication_failure_does_not_retry_or_leak_password(self, factory):
        factory.return_value.connect.side_effect = paramiko.AuthenticationException('testpass')
        with self.assertRaisesRegex(RuntimeError, 'Palo SSH authentication') as error:
            connect_palo(MagicMock(), '172.16.1.134', 'admin', 'testpass', 60)
        self.assertNotIn('testpass', str(error.exception))
        factory.return_value.connect.assert_called_once()
        factory.return_value.close.assert_called_once()

    @patch('eve_lab.initialize.PaloConsole')
    @patch('eve_lab.initialize.connect_palo')
    @patch('eve_lab.initialize.paramiko.SSHClient')
    @patch('eve_lab.initialize.credentials', return_value=['admin', 'testpass'])
    @patch('eve_lab.initialize.load_server', return_value={'url': 'http://10.0.4.4', 'ssh_username': 'root', 'ssh_password': 'test'})
    def test_init_applies_palo_file_over_ssh_and_closes_session(self, server, creds, ssh, connect, console):
        device, channel = MagicMock(), MagicMock()
        connect.return_value = device, channel
        result = initialize(self.client, {'name': 'test'}, self.root, 'default',
                            node_name='PA', management_ip='172.16.1.134')
        console.return_value.initialize.assert_called_once_with(
            ['set deviceconfig system hostname PA'], username='admin', password='testpass')
        self.assertEqual(result['completed'], ['PA'])
        ssh.return_value.get_transport.return_value.open_session.assert_not_called()
        channel.close.assert_called_once()
        device.close.assert_called_once()
