import unittest
from unittest.mock import MagicMock, patch
from eve_lab.device_console import Console


class ConsoleTests(unittest.TestCase):
    def console(self, responses):
        channel = MagicMock()
        channel.recv_ready.return_value = True
        channel.recv.side_effect = [r.encode() for r in responses]
        return Console(channel), channel

    def test_login_with_credentials_and_enable(self):
        c, ch = self.console(['Username:', 'Password:', 'R0>', 'Password:', 'R0#'])
        c.login('admin', 'loginpass', 'enablepass')
        self.assertEqual([v.args[0] for v in ch.sendall.call_args_list],
                         ['\r', 'admin\r', 'loginpass\r', 'enable\r', 'enablepass\r'])

    def test_initial_boot(self):
        c, ch = self.console(['Would you like to enter the initial configuration dialog? [yes/no]:',
                             'Press RETURN to get started!', 'Router>', 'Router#'])
        c.login('admin', 'pass', 'secret')
        self.assertEqual(c.prompt, 'Router#')

    def test_initial_enable_secret_confirmation_and_save(self):
        c, ch = self.console([
            'Would you like to enter the initial configuration dialog? [yes/no]:',
            'Enter enable secret:', 'Confirm enable secret:',
            'Enter your selection [2]:', 'Press RETURN to get started!',
            'Router>', 'Password:', 'Router#'])
        with patch('sys.stderr'):
            c.login('admin', 'loginpass', 'enablepass')
        self.assertEqual([v.args[0] for v in ch.sendall.call_args_list],
                         ['\r', 'no\r', 'enablepass\r', 'enablepass\r', '2\r',
                          '\r', 'enable\r', 'enablepass\r'])
        self.assertEqual(c.prompt, 'Router#')

    def test_initial_secret_rejection_does_not_loop_or_disclose_secret(self):
        c, ch = self.console(['Enter enable secret:', 'Enter enable secret:'])
        with patch('sys.stderr') as stderr:
            with self.assertRaisesRegex(RuntimeError, 'CISCO_ENABLE_SECRET') as error:
                c.login('admin', 'loginpass', 'sensitive-value')
        self.assertNotIn('sensitive-value', str(error.exception))
        self.assertNotIn('sensitive-value', str(stderr.write.call_args_list))
        self.assertEqual(ch.sendall.call_count, 2)

    def test_initial_secret_confirmation_stops_wakeup_enters(self):
        c = Console(MagicMock())
        import re
        c.expect = MagicMock(side_effect=[('', re.match('.*', text)) for text in
                            ['Enter enable secret:', 'Confirm enable secret:', 'Router#']])
        with patch('sys.stderr'):
            c.login('admin', 'loginpass', 'enablepass')
        self.assertEqual([call.kwargs['wake'] for call in c.expect.call_args_list], [True, False, False])

    def test_initial_wait_wakes_console_after_early_enter_is_lost(self):
        channel = MagicMock()
        channel.closed = False
        channel.exit_status_ready.return_value = False
        channel.recv_ready.side_effect = lambda: channel.sendall.called
        channel.recv.return_value = b'Router#'
        c = Console(channel)
        with patch('eve_lab.device_console.time.monotonic', side_effect=[0, 1, 11, 12, 13]), \
                patch('eve_lab.device_console.time.sleep'):
            c.expect(r'Router#', wake=True)
        channel.sendall.assert_called_once_with('\r')

    def test_terminal_controls_and_buffered_prompts(self):
        c, ch = self.console(['\x1b[32mUsername:\x1b[0m\nPassword:'])
        self.assertEqual(c.expect(r'Username:')[1].group(), 'Username:')
        self.assertEqual(c.expect(r'Password:')[1].group(), 'Password:')
        ch.recv.assert_called_once()

    def test_password_wait_does_not_send_extra_enter(self):
        channel = MagicMock()
        channel.closed = False
        channel.exit_status_ready.return_value = False
        channel.recv_ready.return_value = False
        c = Console(channel)
        with patch('eve_lab.device_console.time.monotonic', side_effect=[0, 1, 11, 61]), \
                patch('eve_lab.device_console.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'Timed out'):
                c.expect(r'Password:')
        channel.sendall.assert_not_called()

    def test_backup_strips_echo_and_checks_complete(self):
        c, _ = self.console(['terminal length 0\nR0#',
                            'more system:running-config\nUsing 99 out of 999 bytes\nhostname R0\n!\nend\nR0#'])
        self.assertEqual(c.backup(), 'hostname R0\n!\nend\n')
        c, _ = self.console(['R0#', 'hostname R0\nR0#'])
        with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
            c.backup()

    def test_interface_status_reads_all_addresses_without_dhcp_changes(self):
        c = Console(MagicMock())
        c.command = MagicMock(side_effect=['',
            'Interface IP-Address OK? Method Status Protocol\n'
            'GigabitEthernet1 172.16.1.20 YES DHCP up up\n'
            'GigabitEthernet2 unassigned YES unset administratively down down\n'
            'Loopback0 10.1.1.1 YES manual up up\n'])
        result = c.interface_status()
        self.assertEqual(result[0]['ip_address'], '172.16.1.20')
        self.assertEqual(result[0]['method'], 'DHCP')
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]['ip_address'], '10.1.1.1')
        self.assertEqual([call.args[0] for call in c.command.call_args_list],
                         ['terminal length 0', 'show ip interface brief'])
        c.command = MagicMock(return_value='Interface IP-Address OK? Method Status Protocol\n'
                             'GigabitEthernet2 unassigned YES unset administratively down down\n')
        self.assertEqual(c.interface_status(), [])

    def test_interface_status_rejects_unrecognized_response(self):
        c = Console(MagicMock())
        c.command = MagicMock(return_value='Unexpected output')
        with self.assertRaisesRegex(RuntimeError, 'Unrecognized'):
            c.interface_status()

    def test_init_stops_on_rejected_command_without_saving(self):
        c, ch = self.console(['R0(config)#', '% Invalid input detected at marker\nR0(config)#'])
        with self.assertRaisesRegex(RuntimeError, 'rejected'):
            c.initialize(['bad command'])
        self.assertFalse(any('write memory' in v.args[0] for v in ch.sendall.call_args_list))

    def test_init_saves(self):
        c, ch = self.console(['R0(config)#', 'R1(config)#', 'R1#', 'Building configuration...\n[OK]\nR1#'])
        c.initialize(['hostname R1'])
        self.assertEqual(ch.sendall.call_args.args[0], 'write memory\r')

    def test_init_provisions_local_ssh_login_before_save(self):
        c = Console(MagicMock())
        c.command = MagicMock(return_value='[OK]\n')
        c.initialize(['hostname R0'], username='admin', password='test-password')
        commands = [call.args[0] for call in c.command.call_args_list]
        self.assertEqual(commands[-7:], [
            'configure terminal', 'username admin privilege 15 password 0 test-password',
            'line vty 0 4', 'login local', 'transport input ssh', 'end', 'write memory'])

    def test_init_rejects_credential_command_injection_before_changes(self):
        c = Console(MagicMock())
        c.command = MagicMock()
        with self.assertRaises(ValueError):
            c.initialize(['hostname R0'], username='admin', password='bad\nreload')
        c.command.assert_not_called()
