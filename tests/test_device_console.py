import unittest
from unittest.mock import MagicMock
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

    def test_backup_strips_echo_and_checks_complete(self):
        c, _ = self.console(['terminal length 0\nR0#',
                            'more system:running-config\nUsing 99 out of 999 bytes\nhostname R0\n!\nend\nR0#'])
        self.assertEqual(c.backup(), 'hostname R0\n!\nend\n')
        c, _ = self.console(['R0#', 'hostname R0\nR0#'])
        with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
            c.backup()

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
