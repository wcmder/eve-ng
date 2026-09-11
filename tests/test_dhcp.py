import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eve_lab.dhcp_remote import clear_leases


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

    def test_symlink_rejected(self):
        other = self.root / 'other.leases'
        self.leases.rename(other)
        self.leases.symlink_to(other)
        with self.assertRaisesRegex(RuntimeError, 'regular'):
            self.clear()
        self.assertTrue(other.read_text())

if __name__ == '__main__':
    unittest.main()
