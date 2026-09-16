import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from eve_lab.console_backup import backup, palo_running


class ConsoleBackupTests(unittest.TestCase):
    def test_xml_requires_complete_tree_and_resets_output(self):
        for text in ('<config><devices/></config>\nadmin@pano>', '<config><devices>', 'error', '<config></config>'):
            console = MagicMock()
            console.command.side_effect = lambda cmd, **kwargs: text if cmd == 'show config running' else ''
            if '</config>' in text and '<devices/>' in text:
                self.assertEqual(palo_running(console, 60), '<config><devices/></config>\n')
            else:
                with self.assertRaises(RuntimeError):
                    palo_running(console, 60)
            self.assertEqual(console.command.call_args.args[0], 'set cli op-command-xml-output off')

    @patch('eve_lab.console_backup.paramiko.SSHClient')
    def test_check_is_read_only_and_supports_panorama(self, ssh):
        client = MagicMock()
        client.request.return_value = {'1': {'name': 'pano', 'template': 'panorama', 'status': 2, 'console': 'telnet', 'url': 'telnet://host:32769'}}
        with tempfile.TemporaryDirectory() as temp:
            result = backup(client, {'name': 'lab'}, Path(temp), check=True)
            self.assertEqual(result['supported'][0]['template'], 'panorama')
            self.assertEqual(list(Path(temp).iterdir()), [])
        ssh.assert_not_called()
        self.assertTrue(all(c.args[0] == 'GET' and c.args[1].endswith('/nodes') for c in client.request.call_args_list))

    @patch('eve_lab.console_backup.load_server', return_value={'url': 'http://host', 'ssh_username': 'root', 'ssh_password': 'secret'})
    @patch('eve_lab.console_backup.credentials', return_value=['admin', 'secret', 'enable'])
    @patch('eve_lab.console_backup.paramiko.SSHClient')
    @patch('eve_lab.console_backup.login')
    @patch('eve_lab.console_backup.Console')
    @patch('eve_lab.console_backup.PaloConsole')
    def test_saves_configs_continues_failures_and_never_exports(self, palo, cisco, login, ssh, creds, server):
        client = MagicMock()
        client.request.return_value = {
            '1': {'name': 'rt', 'template': 'c8000v', 'status': 2, 'console': 'telnet', 'url': 'telnet://host:32769'},
            '2': {'name': 'pano', 'template': 'panorama', 'status': 2, 'console': 'telnet', 'url': 'telnet://host:32770'},
            '3': {'name': 'bad', 'template': 'c8000v', 'status': 2, 'console': 'telnet', 'url': 'telnet://host:32771'},
            '4': {'name': 'linux', 'template': 'linux', 'status': 2},
        }
        cisco.return_value.command.side_effect = ['', 'hostname rt\nend\n', '', 'truncated']
        palo.return_value.command.side_effect = ['', '', '<config><devices/></config>\nadmin@pano>', '']
        with tempfile.TemporaryDirectory() as temp:
            result = backup(client, {'name': 'lab'}, Path(temp))
            directory = Path(result['directory'])
            self.assertEqual(len(result['saved']), 2)
            self.assertEqual(result['failed'][0]['node'], 'bad')
            self.assertEqual(result['skipped'][0]['node'], 'linux')
            self.assertEqual((directory / 'rt-1.cfg').read_text(), 'hostname rt\nend\n')
            self.assertEqual((directory / 'pano-2.xml').read_text(), '<config><devices/></config>\n')
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            for f in directory.iterdir():
                self.assertEqual(f.stat().st_mode & 0o777, 0o600)
            manifest = json.loads((directory / 'manifest.json').read_text())
            self.assertEqual(manifest['saved'][1]['format'], 'panos-running-xml')
        self.assertTrue(all(c.args[0] == 'GET' and c.args[1].endswith('/nodes') for c in client.request.call_args_list))
        self.assertEqual(ssh.return_value.get_transport.return_value.open_session.return_value.close.call_count, 3)
        ssh.return_value.close.assert_called_once()
