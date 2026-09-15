import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eve_lab.dhcp_remote import clear_leases, update_dns


class DhcpTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / 'pnet1.conf'
        self.leases = self.root / 'pnet1.leases'
        self.leases.write_text('123 aa:bb:cc:dd:ee:ff 172.16.1.100 host *\n')
        self.config.write_text(f'interface=pnet1\ndhcp-leasefile={self.leases}\n')
        self.commands = []
        def run(*args):
            self.commands.append(args)
            if args[1] == 'show':
                return f'/usr/sbin/dnsmasq --conf-file={self.config}'
            if args[1] == 'is-active':
                return 'active'
            return ''
        self.addCleanup(patch.stopall)
        patch('eve_lab.dhcp_remote.run', side_effect=run).start()
        patch('eve_lab.dhcp_remote.os.geteuid', return_value=0).start()

    def clear(self, dry_run=False):
        return clear_leases(dry_run, self.config, self.leases)

    def test_backup_clear_restart(self):
        original = self.leases.read_text()
        result = self.clear()
        self.assertEqual(Path(result['backup']).read_text(), original)
        self.assertEqual(self.leases.read_text(), '')
        self.assertEqual(result['lease_count'], 1)
        self.assertEqual([cmd[1] for cmd in self.commands], ['show', 'is-active', 'stop', 'start', 'is-active'])

    def test_dry_run_is_read_only(self):
        result = self.clear(True)
        self.assertEqual(result['lease_count'], 1)
        self.assertTrue(self.leases.read_text())
        self.assertFalse(list(self.root.glob('*.backup-*')))
        self.assertFalse(any(cmd[1] in ('start', 'stop') for cmd in self.commands))

    def test_reject_shared_config(self):
        with self.config.open('a') as file:
            file.write('interface=pnet2\n')
        with self.assertRaisesRegex(RuntimeError, 'not dedicated'):
            self.clear()
        self.assertEqual(self.commands, [])

    def test_restart_even_if_backup_fails(self):
        with patch('eve_lab.dhcp_remote.shutil.copy2', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'disk full'):
                self.clear()
        self.assertEqual(self.commands[-1][1], 'start')
        self.assertTrue(self.leases.read_text())

    def test_report_records_without_changes(self):
        original = ('200 aa:bb:cc:dd:ee:ff 172.16.1.100 router id1\n'
                    '0 aa:bb:cc:dd:ee:01 172.16.1.101 * *\n'
                    '50 aa:bb:cc:dd:ee:02 172.16.1.102 old *\n')
        self.leases.write_text(original)
        with patch('eve_lab.dhcp_remote.time.time', return_value=100):
            result = clear_leases(config_path=self.config, lease_path=self.leases, report=True)
        self.assertEqual(result['lease_count'], 3)
        active, permanent, expired = result['leases']
        self.assertEqual(active['remaining_seconds'], 100)
        self.assertEqual(active['status'], 'active')
        self.assertEqual(active['expires_at'], '1970-01-01T00:03:20+00:00')
        self.assertEqual(permanent['status'], 'permanent')
        self.assertIsNone(permanent['hostname'])
        self.assertIsNone(permanent['expires_at'])
        self.assertEqual(expired['status'], 'expired')
        self.assertEqual(expired['remaining_seconds'], 0)
        self.assertEqual(self.leases.read_text(), original)
        self.assertFalse(list(self.root.glob('*.backup-*')))
        self.assertEqual([cmd[1] for cmd in self.commands], ['show', 'is-active'])

    def test_empty_report_and_invalid_record(self):
        self.leases.write_text('')
        result = clear_leases(config_path=self.config, lease_path=self.leases, report=True)
        self.assertEqual(result['leases'], [])
        for record in ('invalid', 'invalid mac ip host client'):
            self.leases.write_text(record)
            with self.assertRaisesRegex(RuntimeError, 'Invalid DHCP lease'):
                clear_leases(config_path=self.config, lease_path=self.leases, report=True)

    def test_symlink_rejected(self):
        other = self.root / 'other.leases'
        self.leases.rename(other)
        self.leases.symlink_to(other)
        with self.assertRaisesRegex(RuntimeError, 'regular'):
            self.clear()
        self.assertTrue(other.read_text())

    def update(self, dry_run=False):
        return update_dns(['8.8.8.8', '1.1.1.1'], dry_run, self.config, self.leases)

    def test_dns_update_backup_preserves_leases_and_is_idempotent(self):
        with self.config.open('a') as stream:
            stream.write('dhcp-option=6\ndhcp-option=option:router,172.16.1.1\n')
        original = self.config.read_text()
        leases = self.leases.read_text()
        result = self.update()
        self.assertTrue(result['changed'])
        self.assertEqual(Path(result['backup']).read_text(), original)
        self.assertEqual(self.leases.read_text(), leases)
        self.assertIn('dhcp-option=option:router,172.16.1.1', self.config.read_text())
        self.assertIn('dhcp-option=option:dns-server,8.8.8.8,1.1.1.1', self.config.read_text())
        self.commands.clear()
        self.assertFalse(self.update()['changed'])
        self.assertFalse(any(c[1] == 'restart' for c in self.commands))

    def test_dns_dry_run_and_invalid_settings_do_not_write(self):
        original = self.config.read_text()
        self.assertTrue(self.update(True)['would_change'])
        self.assertEqual(self.config.read_text(), original)
        self.assertFalse(list(self.root.glob('*.backup-*')))
        for servers in ([], ['bad;command'], ['8.8.8.8', '']):
            with self.assertRaises(ValueError):
                update_dns(servers, config_path=self.config, lease_path=self.leases)

    def test_dns_validation_failure_keeps_original(self):
        original = self.config.read_text()
        with patch('eve_lab.dhcp_remote.run', side_effect=[
            f'dnsmasq --conf-file={self.config}', 'active', RuntimeError('syntax error')]):
            with self.assertRaisesRegex(RuntimeError, 'syntax error'):
                self.update()
        self.assertEqual(self.config.read_text(), original)
        self.assertFalse(list(self.root.glob('.pnet1-dns-*')))

    def test_dns_restart_failure_restores_configuration(self):
        original = self.config.read_text()
        with patch('eve_lab.dhcp_remote.run', side_effect=[
            f'dnsmasq --conf-file={self.config}', 'active', '', RuntimeError('restart failed'), '', 'active']):
            with self.assertRaisesRegex(RuntimeError, 'previous configuration restored'):
                self.update()
        self.assertEqual(self.config.read_text(), original)
        self.assertTrue(self.leases.read_text())

    @patch.dict('os.environ', {}, clear=True)
    @patch('eve_lab.dhcp.run_remote')
    def test_dns_uses_environment_and_rejects_invalid_before_ssh(self, remote):
        from eve_lab.dhcp import update_dns as invoke
        (self.root / '.env').write_text('EVE_DHCP_DNS=8.8.8.8,1.1.1.1\n')
        invoke({}, 'pnet1', self.root, dry_run=True)
        self.assertTrue(remote.call_args.args[1].endswith('--update-dns 8.8.8.8,1.1.1.1 --dry-run'))
        with patch.dict('os.environ', {'EVE_DHCP_DNS': '9.9.9.9'}):
            invoke({}, 'pnet1', self.root)
            self.assertTrue(remote.call_args.args[1].endswith('--update-dns 9.9.9.9'))
        remote.reset_mock()
        with patch.dict('os.environ', {'EVE_DHCP_DNS': '8.8.8.8;command'}):
            with self.assertRaises(ValueError): invoke({}, 'pnet1', self.root)
        remote.assert_not_called()

if __name__ == '__main__':
    unittest.main()
