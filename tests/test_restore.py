import json
from pathlib import Path
from tempfile import TemporaryDirectory
import re
import unittest
from unittest.mock import MagicMock, patch

from eve_lab.device_restore import restore, validate_config, import_file, apply_file, RestoreError
from eve_lab.restore import read_backup


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = 'version 17.15\nhostname R0\nend\n'
        self.entries = [{'node': 'R0', 'template': 'c8000v', 'image': 'c8000v-test', 'ethernet': 4,
                         'format': 'ios-running-config', 'file': 'R0.cfg'}]
        (self.root / 'R0.cfg').write_text(self.config)
        self.manifest()
        self.nodes = {'7': {'name': 'R0', 'template': 'c8000v', 'image': 'c8000v-test', 'ethernet': 4,
                            'status': 2, 'console': 'telnet', 'url': 'telnet://host:32769'}}
        self.client = MagicMock()
        def request(method, path, payload=None):
            self.assertEqual(method, 'GET')
            self.assertTrue(path.endswith('/nodes'))
            return self.nodes
        self.client.request.side_effect = request

    def manifest(self):
        (self.root / 'manifest.json').write_text(json.dumps({'saved': self.entries}))

    def run_restore(self, **kwargs):
        return restore(self.client, {'name': 'lab'}, self.root, self.root, **kwargs)

    @patch('eve_lab.device_restore.paramiko.SSHClient')
    def test_check_no_device_or_file_changes(self, ssh):
        result = self.run_restore(check=True)
        self.assertEqual(result['planned'][0]['node'], 'R0')
        ssh.assert_not_called()
        self.nodes['7']['status'] = 0
        self.assertFalse(self.run_restore(check=True)['planned'][0]['running'])
        with self.assertRaisesRegex(ValueError, 'Start'):
            self.run_restore()

    def test_compatibility_and_complete_files_before_login(self):
        for key in ('template', 'image', 'ethernet'):
            old = self.nodes['7'][key]
            self.nodes['7'][key] = 'mismatch'
            with self.assertRaisesRegex(ValueError, key):
                self.run_restore(check=True)
            self.nodes['7'][key] = old
        (self.root / 'R0.cfg').write_text('hostname R0\n')
        with self.assertRaisesRegex(ValueError, 'complete'):
            self.run_restore(check=True)

    def test_bad_paths_rejected(self):
        self.entries[0]['file'] = '../outside.cfg'
        self.manifest()
        with self.assertRaises(ValueError):
            self.run_restore()
        self.client.request.assert_not_called()

    def test_panos_xml_validation(self):
        entry = {'template': 'panorama', 'format': 'panos-running-xml'}
        validate_config(entry, '<config><devices/></config>')
        for config in ('<config>', '<config/>', '<wrong/>', '<!DOCTYPE config><config><devices/></config>'):
            with self.assertRaises(ValueError): validate_config(entry, config)

    def console(self, responses):
        console = MagicMock()
        iterator = iter(responses)
        def expect(pattern, **kwargs):
            output = next(iterator)
            match = re.search(pattern, output, re.M)
            self.assertIsNotNone(match, output)
            return output, match
        console.expect.side_effect = expect
        return console

    def test_scp_hostkey_and_password_handshake(self):
        console = self.console(['RSA key fingerprint is SHA256:trusted.\nAre you sure you want to continue connecting (yes/no/[fingerprint])?',
                                "root@172.16.1.1's password:", 'file.xml 100% 2000\nadmin@pano>'])
        import_file(console, 'panorama', 'root', '172.16.1.1', '/tmp/safe/file.xml', 'file.xml', 'secret', {'SHA256:trusted'}, 60)
        self.assertEqual([c.args[0] for c in console.send.call_args_list][1:], ['yes', 'secret'])
        console = self.console(['RSA key fingerprint is SHA256:wrong.\nAre you sure you want to continue connecting (yes/no)?'])
        with self.assertRaisesRegex(RestoreError, 'fingerprint'):
            import_file(console, 'panorama', 'root', '172.16.1.1', '/tmp/file.xml', 'file.xml', 'secret', {'SHA256:trusted'}, 60)
        self.assertEqual(console.send.call_args.args[0], 'no')
        self.assertNotIn('secret', [c.args[0] for c in console.send.call_args_list])

    def test_scp_failed_or_repeated_password_never_applies(self):
        for responses in (["Password:", "Password:"], ['Permission denied\nadmin@pano>'], ['admin@pano>']):
            console = self.console(responses)
            with self.assertRaises(RestoreError):
                import_file(console, 'panorama', 'root', '172.16.1.1', '/tmp/file.xml', 'file.xml', 'secret', set(), 60)

    def test_pano_load_commit_confirmation(self):
        console = MagicMock()
        console.command.side_effect = ['', 'Configuration loaded successfully', 'Configuration committed successfully', '']
        apply_file(console, 'panorama', 'restore.xml', '<config/>', 600)
        self.assertEqual([c.args[0] for c in console.command.call_args_list], ['configure', 'load config from restore.xml', 'commit', 'exit'])
        console.reset_mock()
        console.command.side_effect = ['', 'Invalid config']
        with self.assertRaisesRegex(RestoreError, 'load'):
            apply_file(console, 'panorama', 'restore.xml', '<config/>', 600)
        self.assertNotIn('commit', [c.args[0] for c in console.command.call_args_list])

    def test_cisco_verifies_before_replace_and_save(self):
        import hashlib
        console = MagicMock()
        console.command.side_effect = [hashlib.md5(self.config.encode()).hexdigest(), 'Rollback Done', '[OK]']
        apply_file(console, 'c8000v', 'restore.cfg', self.config, 600)
        self.assertEqual(console.command.call_args.args[0], 'write memory')
        console.reset_mock()
        console.command.side_effect = ['wrong hash']
        with self.assertRaisesRegex(RestoreError, 'checksum'):
            apply_file(console, 'c8000v', 'restore.cfg', self.config, 600)
        self.assertEqual(console.command.call_count, 1)

    @patch('eve_lab.device_restore.apply_file')
    @patch('eve_lab.device_restore.import_file')
    @patch('eve_lab.device_restore.login')
    @patch('eve_lab.device_restore.Console')
    @patch('eve_lab.device_restore.trusted_fingerprints', return_value={'SHA256:key'})
    @patch('eve_lab.device_restore.host_details', return_value='172.16.1.1')
    @patch('eve_lab.device_restore.paramiko.SSHClient')
    @patch('eve_lab.device_restore.credentials', return_value=['admin', 'pass', 'enable'])
    @patch('eve_lab.device_restore.load_server', return_value={'url': 'http://host', 'ssh_username': 'root', 'ssh_password': 'secret'})
    def test_restore_stages_verifies_cleans_and_reports_failure(self, server, creds, ssh, host, keys, console, login, transfer, apply):
        sftp = ssh.return_value.open_sftp.return_value
        sftp.open.return_value.__enter__.return_value.read.return_value = self.config.encode()
        result = self.run_restore()
        self.assertEqual(result['completed'], ['R0'])
        transfer.assert_called_once()
        apply.assert_called_once()
        sftp.remove.assert_called_once()
        sftp.rmdir.assert_called_once()
        ssh.return_value.close.assert_called_once()
        self.assertTrue(sftp.mkdir.call_args.kwargs['mode'] == 0o700)
        apply.side_effect = RestoreError('Save failed')
        result = self.run_restore()
        self.assertEqual(result['completed'], [])
        self.assertEqual(result['failed'][0]['reason'], 'Save failed')
