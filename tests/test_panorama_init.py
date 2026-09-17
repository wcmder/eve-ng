from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from eve_lab.initialize import PanoramaConsole, initialize, prepare_panorama_console


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
        console = self.console(iter(['Panorama login:', 'Password:', 'Login incorrect', 'Panorama login:', 'Password:', 'Enter old password :',
                                     'Enter new password :', 'Confirm password :', 'admin@Panorama>']))
        console.login('admin', 'new-secret')
        self.assertEqual([c.args[0] for c in console.send.call_args_list],
                         ['', 'admin', 'new-secret', 'admin', 'admin', 'admin', 'new-secret', 'new-secret'])

    def test_rejected_password_is_not_retried_or_exposed(self):
        console = self.console(iter(['login:', 'Password:', 'New password:', 'New password:']))
        with self.assertRaisesRegex(RuntimeError, 'repeated') as error:
            console.login('admin', 'new-secret')
        self.assertNotIn('new-secret', str(error.exception))

    def test_initialized_device_uses_configured_password_without_fallback(self):
        console = self.console(iter(['Panorama login:', 'Password:', 'admin@pano>']))
        console.login('admin', 'new-secret')
        self.assertEqual([c.args[0] for c in console.send.call_args_list],
                         ['', 'admin', 'new-secret'])

    def test_factory_fallback_is_attempted_only_once(self):
        console = self.console(iter(['login:', 'Password:', 'Login incorrect',
                                     'login:', 'Password:', 'Login incorrect']))
        with self.assertRaisesRegex(RuntimeError, 'configured and factory credentials'):
            console.login('admin', 'new-secret')
        self.assertEqual([c.args[0] for c in console.send.call_args_list],
                         ['', 'admin', 'new-secret', 'admin', 'admin'])

    def test_abandoned_login_failure_before_our_credentials_is_ignored_once(self):
        console = self.console(iter(['Login incorrect', 'Panorama login:', 'Password:', 'admin@pano>']))
        console.login('admin', 'new-secret')
        self.assertEqual([c.args[0] for c in console.send.call_args_list],
                         ['', 'admin', 'new-secret'])

    def test_factory_fallback_requires_password_change(self):
        console = self.console(iter(['login:', 'Password:', 'Login incorrect', 'login:', 'Password:', 'admin@Panorama>']))
        with self.assertRaisesRegex(RuntimeError, 'did not confirm'):
            console.login('admin', 'new-secret')

    @patch('eve_lab.initialize.paramiko.SSHClient')
    def test_preview_and_factory_preflight(self, ssh):
        result = self.init(check=True)
        self.assertEqual(result['planned'][0]['transport'], 'telnet')
        ssh.assert_not_called()
        result = self.init(check=True, management_ip='172.16.1.50')
        self.assertEqual(result['planned'][0]['transport'], 'ssh')
        self.config.write_text('set deviceconfig system hostname pano\n')
        with self.assertRaisesRegex(ValueError, 'static'):
            self.init(check=True)
        self.config.write_text(NETWORK.replace('172.16.1.1 ', '172.16.2.1 '))
        with self.assertRaisesRegex(ValueError, 'Invalid Panorama'):
            self.init(check=True)

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

    def test_firewall_prepare_console_has_same_stopped_and_read_only_guards(self):
        self.node['template'] = 'paloalto'
        self.test_prepare_console_requires_stopped_node_and_check_is_read_only()

    @patch('eve_lab.initialize.PaloSerialConsole')
    @patch('eve_lab.initialize.paramiko.SSHClient')
    @patch('eve_lab.initialize.credentials', return_value=['admin', 'new-secret'])
    @patch('eve_lab.initialize.load_server', return_value={'url': 'http://10.0.4.4', 'ssh_username': 'root', 'ssh_password': 'test'})
    def test_firewall_serial_init_uses_first_login_and_its_config(self, server, creds, ssh, console):
        self.node['template'] = 'paloalto'
        commands = ['set deviceconfig system hostname pa-a',
                    'set deviceconfig system type dhcp-client']
        self.config.write_text('\n'.join(commands))
        result = self.init()
        self.assertEqual(result['completed'], ['pano'])
        console.return_value.login.assert_called_once_with('admin', 'new-secret', auto_factory=True)
        console.return_value.initialize.assert_called_once_with(commands, username='admin', password='new-secret')
        channel = ssh.return_value.get_transport.return_value.open_session.return_value
        channel.exec_command.assert_called_once_with('telnet 127.0.0.1 32772')
        channel.close.assert_called_once()

    @patch('eve_lab.initialize.PanoramaConsole')
    @patch('eve_lab.initialize.paramiko.SSHClient')
    @patch('eve_lab.initialize.credentials', return_value=['admin', 'new-secret'])
    @patch('eve_lab.initialize.load_server', return_value={'url': 'http://10.0.4.4', 'ssh_username': 'root', 'ssh_password': 'test'})
    def test_execution_preserves_configured_hostname_and_services(self, server, creds, ssh, console):
        (self.root / '.env').write_text('PANORAMA_MANAGEMENT_IP=172.16.1.99\nPANORAMA_DNS=invalid\n')
        configured = [NETWORK.strip(), 'set deviceconfig system dns-setting servers secondary 8.8.8.8',
                      'set deviceconfig system hostname custom-panorama',
                      'set deviceconfig system service disable-ssh yes']
        self.config.write_text('\n'.join(configured))
        result = self.init()
        self.assertEqual(result['completed'], ['pano'])
        console.return_value.login.assert_called_once_with('admin', 'new-secret', auto_factory=True)
        commands = console.return_value.initialize.call_args.args[0]
        self.assertEqual(commands, configured)
        channel = ssh.return_value.get_transport.return_value.open_session.return_value
        channel.exec_command.assert_called_once_with('telnet 127.0.0.1 32772')
        channel.close.assert_called_once()

    def test_commit_must_be_confirmed(self):
        console = PanoramaConsole(MagicMock())
        console.command = MagicMock(return_value='Commit job queued')
        with self.assertRaisesRegex(RuntimeError, 'commit not confirmed'):
            console.initialize([NETWORK.strip()])

    def test_network_environment_cannot_replace_missing_config(self):
        (self.root / '.env').write_text('PANORAMA_MANAGEMENT_IP=172.16.1.99\n'
                                      'PANORAMA_NETMASK=255.255.255.0\n'
                                      'PANORAMA_GATEWAY=172.16.1.1\n'
                                      'PANORAMA_DNS=8.8.8.8,1.1.1.1\n')
        self.config.write_text('# No network configuration\n')
        with self.assertRaisesRegex(ValueError, 'in its init file'):
            self.init(check=True)

    @patch('eve_lab.cli.prepare_panorama_console', return_value={'changed': False, 'check': True})
    @patch('eve_lab.cli.load_lab_target', return_value={'name': 'test'})
    @patch('eve_lab.cli.load_server', return_value={'url': 'http://example.invalid', 'timeout': 10, 'username': 'admin', 'password': 'test'})
    @patch('eve_lab.cli.EveClient')
    def test_cli_prepare_console_accepts_result_without_failed_list(self, client, server, target, prepare):
        from eve_lab.cli import main
        with patch('sys.argv', ['eve', 'init', 'test', '--node', 'pano', '--prepare-console', '--check']), patch('builtins.print'):
            main()
        prepare.assert_called_once_with(client.return_value, {'name': 'test'}, 'pano', True)
