from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from eve_lab.initialize import PanoramaConsole, initialize, prepare_panorama_console, panorama_network_commands


NETWORK = ('set deviceconfig system ip-address 172.16.1.50 netmask 255.255.255.0 '
           'default-gateway 172.16.1.1 dns-setting servers primary 1.1.1.1\n')


class PanoramaInitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'labs/test/configs/pano-init.cfg'
        self.config.parent.mkdir(parents=True)
        self.config.write_text(NETWORK)
        self.node = {'name': 'pano', 'template': 'panorama', 'console': 'telnet',
                     'status': 2, 'url': 'telnet://10.0.4.4:32772'}
        self.client = MagicMock()
        def request(method, path, payload=None):
            if path.endswith('/nodes'):
                return {'4': self.node.copy()}
            if method == 'PUT':
                self.node.update(payload)
            return self.node.copy()
        self.client.request.side_effect = request

    def init(self, **kwargs):
        return initialize(self.client, {'name': 'test'}, self.root, 'default',
                          node_name='pano', **kwargs)

    def console(self, prompts):
        console = PanoramaConsole(MagicMock())
        console.send = MagicMock()
        def expect(pattern, **kwargs):
            import re
            text = next(prompts)
            match = re.search(pattern, text, re.M)
            self.assertIsNotNone(match, text)
            return text, match
        console.expect = expect
        return console

    def test_factory_password_change_sequence(self):
        console = self.console(iter(['Panorama login:', 'Password:', 'Enter old password :',
                                     'Enter new password :', 'Confirm password :', 'admin@Panorama>']))
        console.login('admin', 'new-secret', factory_default=True)
        self.assertEqual([c.args[0] for c in console.send.call_args_list],
                         ['', 'admin', 'admin', 'admin', 'new-secret', 'new-secret'])

    def test_rejected_password_is_not_retried_or_exposed(self):
        console = self.console(iter(['login:', 'Password:', 'New password:', 'New password:']))
        with self.assertRaisesRegex(RuntimeError, 'repeated') as error:
            console.login('admin', 'new-secret', factory_default=True)
        self.assertNotIn('new-secret', str(error.exception))

    def test_factory_mode_does_not_accept_shell_without_password_change(self):
        console = self.console(iter(['admin@Panorama>']))
        with self.assertRaisesRegex(RuntimeError, 'did not confirm'):
            console.login('admin', 'new-secret', factory_default=True)

    @patch('eve_lab.initialize.paramiko.SSHClient')
    def test_preview_and_factory_preflight(self, ssh):
        result = self.init(check=True, factory_default=True)
        self.assertEqual(result['planned'][0]['transport'], 'telnet')
        ssh.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'serial console'):
            self.init(check=True, factory_default=True, management_ip='172.16.1.50')
        self.config.write_text('set deviceconfig system hostname pano\n')
        with self.assertRaisesRegex(ValueError, 'static'):
            self.init(check=True, factory_default=True)
        self.config.write_text(NETWORK.replace('172.16.1.1 ', '172.16.2.1 '))
        with self.assertRaisesRegex(ValueError, 'Invalid Panorama'):
            self.init(check=True, factory_default=True)

    def test_prepare_console_requires_stopped_node_and_check_is_read_only(self):
        with self.assertRaisesRegex(ValueError, 'Stop'):
            prepare_panorama_console(self.client, {'name': 'test'}, 'pano')
        self.node.update(status=0, console='vnc')
        result = prepare_panorama_console(self.client, {'name': 'test'}, 'pano', check=True)
        self.assertFalse(result['changed'])
        self.assertTrue(all(c.args[0] == 'GET' for c in self.client.request.call_args_list))
        result = prepare_panorama_console(self.client, {'name': 'test'}, 'pano')
        self.assertTrue(result['changed'])
        self.assertEqual(self.node['console'], 'telnet')
        self.assertFalse(prepare_panorama_console(self.client, {'name': 'test'}, 'pano')['changed'])
        self.assertFalse(any(c.args[1].endswith(('/start', '/wipe')) for c in self.client.request.call_args_list))

    @patch('eve_lab.initialize.PanoramaConsole')
    @patch('eve_lab.initialize.paramiko.SSHClient')
    @patch('eve_lab.initialize.credentials', return_value=['admin', 'new-secret'])
    @patch('eve_lab.initialize.load_server', return_value={'url': 'http://10.0.4.4', 'ssh_username': 'root', 'ssh_password': 'test'})
    def test_execution_uses_console_and_applies_management_services(self, server, creds, ssh, console):
        result = self.init(factory_default=True)
        self.assertEqual(result['completed'], ['pano'])
        console.return_value.login.assert_called_once_with('admin', 'new-secret', factory_default=True)
        commands = console.return_value.initialize.call_args.args[0]
        self.assertIn('set deviceconfig system hostname pano', commands)
        self.assertIn('set deviceconfig system service disable-ssh no', commands)
        channel = ssh.return_value.get_transport.return_value.open_session.return_value
        channel.exec_command.assert_called_once_with('telnet 127.0.0.1 32772')
        channel.close.assert_called_once()

    def test_commit_must_be_confirmed(self):
        console = PanoramaConsole(MagicMock())
        console.command = MagicMock(return_value='Commit job queued')
        with self.assertRaisesRegex(RuntimeError, 'commit not confirmed'):
            console.initialize([NETWORK.strip()])

    @patch.dict('os.environ', {}, clear=True)
    def test_network_env_validated_and_shell_overrides_file(self):
        settings = ('PANORAMA_MANAGEMENT_IP=172.16.1.50\nPANORAMA_NETMASK=255.255.255.0\n'
                    'PANORAMA_GATEWAY=172.16.1.1\nPANORAMA_DNS=1.1.1.1\n')
        (self.root / '.env').write_text(settings)
        self.config.write_text('# Optional additional commands\n')
        self.assertEqual(self.init(check=True, factory_default=True)['planned'][0]['node'], 'pano')
        with patch.dict('os.environ', {'PANORAMA_MANAGEMENT_IP': '172.16.1.51'}):
            self.assertIn('ip-address 172.16.1.51 ', panorama_network_commands(self.root)[0])
        for invalid in ('bad;command', '172.16.1.50\ncommit'):
            with patch.dict('os.environ', {'PANORAMA_MANAGEMENT_IP': invalid}):
                with self.assertRaises(ValueError):
                    panorama_network_commands(self.root)
        (self.root / '.env').write_text('PANORAMA_MANAGEMENT_IP=172.16.1.50\n')
        with self.assertRaisesRegex(ValueError, 'Set all'):
            self.init(check=True, factory_default=True)

    @patch('eve_lab.cli.prepare_panorama_console', return_value={'changed': False, 'check': True})
    @patch('eve_lab.cli.load_lab_target', return_value={'name': 'test'})
    @patch('eve_lab.cli.load_server', return_value={'url': 'http://example.invalid', 'timeout': 10, 'username': 'admin', 'password': 'test'})
    @patch('eve_lab.cli.EveClient')
    def test_cli_prepare_console_accepts_result_without_failed_list(self, client, server, target, prepare):
        from eve_lab.cli import main
        with patch('sys.argv', ['eve', 'init', 'test', '--node', 'pano', '--prepare-console', '--check']), patch('builtins.print'):
            main()
        prepare.assert_called_once_with(client.return_value, {'name': 'test'}, 'pano', True)
