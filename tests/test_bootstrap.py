import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET

from eve_lab.bootstrap import bootstrap_files, prepare, without_managed_cdrom


HASH = '$1$testsalt$abcdefghijklmnopqrstuv'


class BootstrapTests(unittest.TestCase):
    @patch('eve_lab.bootstrap.subprocess.run')
    def test_package_has_hashed_admin_and_dhcp(self, run):
        run.return_value = MagicMock(returncode=0, stdout=HASH + '\n')
        files = bootstrap_files('pa-a', 'admin', 'private-password')
        root = ET.fromstring(files['config/bootstrap.xml'])
        self.assertEqual(root.find('./mgt-config/users/entry/phash').text, HASH)
        self.assertEqual(root.find('.//role-based/superuser').text, 'yes')
        self.assertEqual(root.find('.//service/disable-ssh').text, 'no')
        self.assertIn('type=dhcp-client\n', files['config/init-cfg.txt'])
        self.assertEqual(root.find('.//hostname').text, 'pa-a')
        self.assertIn('hostname=pa-a\n', files['config/init-cfg.txt'])
        self.assertNotIn('private-password', ''.join(files.values()))
        self.assertNotIn('private-password', str(run.call_args.args))

    def test_rejects_invalid_identifiers_and_passwords(self):
        for username, password in [('bad/name', 'valid'), ('admin', 'bad\nvalue')]:
            with self.assertRaises(ValueError): bootstrap_files('pa-a', username, password)
        with self.assertRaises(ValueError): bootstrap_files('bad\nhostname', 'admin', 'valid-password')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.detail = {'name': 'pa-a', 'template': 'paloalto', 'image': 'paloalto-11.2.10-h6',
                       'status': 0, 'qemu_options': '-machine type=pc,accel=kvm -cpu host'}
        self.client = MagicMock()
        def request(method, path, payload=None):
            if path.endswith('/nodes'): return {'6': self.detail.copy()}
            if method == 'PUT':
                self.detail.update(payload)
                return None
            return self.detail.copy()
        self.client.request.side_effect = request

    def run_prepare(self, **kwargs):
        return prepare(self.client, {'name': 'palo-lab'}, self.root, 'default', 'pa-a', **kwargs)

    @patch('eve_lab.bootstrap.paramiko.SSHClient')
    def test_check_is_read_only(self, ssh):
        result = self.run_prepare(check=True, attach=True)
        self.assertTrue(result['check'])
        self.assertEqual(list(self.root.iterdir()), [])
        ssh.assert_not_called()
        self.assertTrue(all(c.args[0] == 'GET' for c in self.client.request.call_args_list))

    def test_attach_refuses_running_or_existing_cdrom(self):
        self.detail['status'] = 2
        with self.assertRaisesRegex(ValueError, 'Stop'):
            self.run_prepare(attach=True)
        self.detail['status'] = 0
        self.detail['qemu_options'] += ' -cdrom /existing.iso'
        with self.assertRaisesRegex(ValueError, 'unrecognized CD-ROM'):
            self.run_prepare(attach=True)

    @patch('eve_lab.bootstrap.paramiko.SSHClient')
    @patch('eve_lab.bootstrap.load_server', return_value={'url': 'http://10.0.4.4', 'ssh_username': 'root', 'ssh_password': 'test'})
    @patch('eve_lab.bootstrap.credentials', return_value=['admin', 'secret'])
    @patch('eve_lab.bootstrap.bootstrap_files', return_value={'config/init-cfg.txt': 'type=dhcp-client\n', 'config/bootstrap.xml': '<config/>'})
    def test_prepare_and_attach_preserves_options_and_never_starts_or_wipes(self, files, creds, server, factory):
        ssh = factory.return_value
        sftp = ssh.open_sftp.return_value.__enter__.return_value
        sftp.stat.return_value.st_size = 40000
        sftp.get.side_effect = lambda remote, local: Path(local).write_bytes(b'iso-test')
        stdout = MagicMock()
        stdout.channel.recv_exit_status.return_value = 0
        ssh.exec_command.return_value = MagicMock(), stdout, MagicMock()
        result = self.run_prepare(attach=True)
        self.assertTrue(result['attached'])
        self.assertTrue(self.detail['qemu_options'].startswith(result['previous_qemu_options']))
        self.assertIn('media=cdrom,if=ide,index=2,readonly=on', self.detail['qemu_options'])
        self.assertTrue(result['remote_iso'].startswith('/opt/unetlab/addons/qemu/.eve-bootstrap/'))
        self.assertNotIn('/paloalto-11.2.', result['remote_iso'])
        manifest = Path(result['directory']) / 'manifest.json'
        self.assertTrue(json.loads(manifest.read_text())['attached'])
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
        self.assertFalse(any(c.args[1].endswith(('/start', '/wipe')) for c in self.client.request.call_args_list))

    def test_replaces_only_recognized_managed_cdrom_preserving_other_flags(self):
        base = '-machine type=pc,accel=kvm -cpu host'
        for root in ('/opt/unetlab/bootstrap', '/opt/unetlab/addons/qemu/.eve-bootstrap'):
            options = base + ' -drive file=' + root + '/092e776f40d9d400471bf4d1/20260912T171448666377Z/cdrom.iso,media=cdrom,if=ide,readonly=on'
            self.assertEqual(without_managed_cdrom(options), base)
            self.assertEqual(without_managed_cdrom(options.replace('if=ide,', 'if=ide,index=2,')), base)
            with self.assertRaises(ValueError):
                without_managed_cdrom(options.replace('if=ide,', 'if=ide,index=1,'))
        with self.assertRaises(ValueError):
            without_managed_cdrom(base + ' -drive file=/someone/else.iso,media=cdrom')
