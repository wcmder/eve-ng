import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from eve_lab.session_discovery import discover, login, palo_identity, versa_login, versa_identity


class DiscoveryTests(unittest.TestCase):
    def test_palo_identity(self):
        self.assertEqual(palo_identity('hostname: pano\nip-address: 172.16.1.99\n'), ('pano', '172.16.1.99'))
        for output in ('hostname: pano', 'hostname: pano\nip-address: 0.0.0.0'):
            with self.assertRaises(RuntimeError):
                palo_identity(output)

    def console(self, prompts):
        console = MagicMock()
        sequence = iter(prompts)
        def expect(pattern, **kwargs):
            text = next(sequence)
            match = re.search(pattern, text, re.M)
            self.assertIsNotNone(match)
            return text, match
        console.expect.side_effect = expect
        return console

    def test_discovery_never_answers_setup_or_retries_failed_login(self):
        for prompts in (['Enter new password:'], ['Enter enable secret:'],
                        ['PA-VM login:', 'Password:', 'Login incorrect']):
            console = self.console(prompts)
            with self.assertRaises(RuntimeError):
                login(console, 'paloalto', ['admin', 'test-password'])
            self.assertNotIn('configure', [c.args[0] for c in console.send.call_args_list])
            if len(prompts) == 1:
                console.send.assert_called_once_with('')

    def test_cisco_enable_login(self):
        console = self.console(['Username:', 'Password:', 'router>', 'Password:', 'router#'])
        login(console, 'c8000v', ['admin', 'password', 'secret'])
        self.assertEqual(console.prompt, 'router#')
        self.assertEqual([c.args[0] for c in console.send.call_args_list], ['', 'admin', 'password', 'enable', 'secret'])

    def test_cisco_buffered_prompt_before_enable_password(self):
        console = self.console(['router>', 'router>', 'Password:', 'router#'])
        login(console, 'c8000v', ['admin', 'password', 'secret'])
        self.assertEqual(console.prompt, 'router#')
        self.assertEqual([c.args[0] for c in console.send.call_args_list],
                         ['', 'enable', 'secret'])

    def test_cisco_rejection_after_secret_never_retries(self):
        for last in ('router>', 'Password:', '% Access denied', '% Bad secrets'):
            console = self.console(['router>', 'Password:', last])
            with self.assertRaises(RuntimeError):
                login(console, 'c8000v', ['admin', 'password', 'secret'])
            self.assertEqual([c.args[0] for c in console.send.call_args_list],
                             ['', 'enable', 'secret'])

    def test_cisco_repeated_unprivileged_prompts_are_bounded(self):
        console = self.console(['router>'] * 4)
        with self.assertRaisesRegex(RuntimeError, 'password prompt not received'):
            login(console, 'c8000v', ['admin', 'password', 'secret'])
        self.assertEqual([c.args[0] for c in console.send.call_args_list], ['', 'enable'])

    def test_versa_login_from_login_and_cli(self):
        console = self.console(['versa login:', 'Password:', 'admin@versa>', '[admin@versa: ~] $'])
        versa_login(console, ['admin', 'password'])
        self.assertEqual([c.args[0] for c in console.send.call_args_list],
                         ['', 'admin', 'password', 'exit'])

    def test_versa_discovery_never_answers_setup_or_password_change(self):
        for prompt in ['New password:', '(current) UNIX password:',
                       'Do you want to enter setup? (y/n)', 'Login incorrect',
                       'admin@versa(config)%', '[other@versa: ~] $']:
            with self.subTest(prompt=prompt):
                console = self.console([prompt])
                with self.assertRaises(RuntimeError):
                    versa_login(console, ['admin', 'password'])
                console.send.assert_called_once_with('')

    def test_versa_identity_reads_only_management_ipv4(self):
        console = self.console([
            'hostname\nversa-director\n[Administrator@versa-director: ~] $',
            'ip -o -4 addr show dev eth0 scope global\n'
            '2: eth0    inet 192.0.2.10/24 brd 192.0.2.255 scope global eth0\n'
            '[Administrator@versa-director: ~] $'])
        self.assertEqual(versa_identity(console), ('versa-director', ['192.0.2.10']))

    @patch('eve_lab.session_discovery.paramiko.SSHClient')
    @patch('eve_lab.session_discovery.credentials', return_value=['admin', 'password'])
    @patch('eve_lab.session_discovery.versa_login')
    @patch('eve_lab.session_discovery.versa_identity', return_value=('versa', ['192.0.2.10']))
    def test_all_versa_templates_discover_with_matching_credentials(self, identity, auth, creds, ssh):
        for template, prefix, port in [('versafvnf', 'VERSA', 'eth-0/0'),
                                       ('versavnf', 'VERSA', 'eth-0/0'),
                                       ('versadir', 'VERSA_DIRECTOR', 'eth0'),
                                       ('versaana', 'VERSA_ANALYTICS', 'eth0')]:
            client = MagicMock()
            client.request.side_effect = [
                {'1': {'name': 'node', 'template': template, 'status': 2,
                       'console': 'telnet', 'url': 'telnet://host:32769'}},
                {'7': {'name': 'mgmt', 'type': 'pnet1'}},
                {'ethernet': {'0': {'name': port, 'network_id': 7}}}]
            result = discover(client, {'name': 'lab'}, Path('.'),
                              {'url': 'http://host', 'ssh_username': 'root', 'ssh_password': 'secret'})
            self.assertEqual(result['skipped'], [])
            self.assertEqual(result['leases'][0]['ip_address'], '192.0.2.10')
            creds.assert_called_with(Path('.'), prefix=prefix)

    @patch('eve_lab.session_discovery.paramiko.SSHClient')
    @patch('eve_lab.session_discovery.credentials', return_value=['admin', 'password', 'secret'])
    @patch('eve_lab.session_discovery.login')
    @patch('eve_lab.session_discovery.Console')
    def test_cisco_selects_only_pnet1_address_and_closes_channel(self, console, auth, creds, ssh):
        client = MagicMock()
        def request(method, path):
            if path.endswith('/nodes'):
                return {'1': {'name': 'node-name', 'template': 'c8000v', 'status': 2, 'console': 'telnet', 'url': 'telnet://host:32769'}}
            if path.endswith('/networks'):
                return {'7': {'name': 'mgmt', 'type': 'pnet1'}}
            return {'ethernet': {'0': {'name': 'Gi1', 'network_id': 7}, '1': {'name': 'Gi2', 'network_id': 8}}}
        client.request.side_effect = request
        console.return_value.prompt = 'actual-host#'
        console.return_value.interface_status.return_value = [
            {'interface': 'GigabitEthernet1', 'ip_address': '172.16.1.100', 'status': 'up', 'protocol': 'up'},
            {'interface': 'GigabitEthernet2', 'ip_address': '10.0.0.1', 'status': 'up', 'protocol': 'up'}]
        result = discover(client, {'name': 'lab'}, Path('.'), {'url': 'http://host', 'ssh_username': 'root', 'ssh_password': 'secret'})
        self.assertEqual(result['leases'], [{'hostname': 'actual-host', 'ip_address': '172.16.1.100', 'status': 'active'}])
        self.assertEqual(result['skipped'], [])
        from eve_lab.session_discovery import DiscoveryError
        auth.side_effect = DiscoveryError('Console is in configuration mode; exit it before discovery')
        failed = discover(client, {'name': 'lab'}, Path('.'), {'url': 'http://host', 'ssh_username': 'root', 'ssh_password': 'secret'})
        self.assertEqual(failed['skipped'][0]['stage'], 'logging into console')
        self.assertIn('configuration mode', failed['skipped'][0]['reason'])
        auth.side_effect = RuntimeError('sensitive-console-output')
        failed = discover(client, {'name': 'lab'}, Path('.'), {'url': 'http://host', 'ssh_username': 'root', 'ssh_password': 'secret'})
        self.assertNotIn('sensitive-console-output', str(failed))
        ssh.return_value.get_transport.return_value.open_session.return_value.close.assert_called()
        ssh.return_value.close.assert_called()
