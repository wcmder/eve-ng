import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from eve_lab.backup import backup
from eve_lab.client import EveAPIError


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = MagicMock()
        self.topology = {'name': 'palo-lab', 'remote_folder': '/'}
        self.nodes = {'1': {'name': 'R0', 'template': 'c8000v'},
                      '2': {'name': 'PA-A', 'template': 'paloalto'}}
        self.export_error = None
        self.config = 'hostname R0\ninterface GigabitEthernet1\n'
        def request(method, path):
            if path.endswith('/nodes'): return self.nodes
            if path.endswith('/configs'): return {'1': {'name': 'R0', 'config': '0'}}
            if path.endswith('/export'):
                self.assertEqual(method, 'PUT')
                if self.export_error: raise self.export_error
                return None
            if path.endswith('/configs/1'): return {'id': 1, 'name': 'R0', 'data': self.config}
            raise AssertionError((method, path))
        self.client.request.side_effect = request
        self.addCleanup(patch.stopall)
        self.stderr = patch('sys.stderr').start()

    def test_exports_supported_and_skips_unsupported(self):
        result = backup(self.client, self.topology, self.root)
        directory = Path(result['directory'])
        target = directory / result['saved'][0]['file']
        self.assertEqual(target.read_text(), self.config)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(result['skipped'][0]['node'], 'PA-A')
        self.assertEqual(result['failed'], [])
        self.assertEqual(json.loads((directory / 'manifest.json').read_text())['saved'], result['saved'])
        self.assertFalse(any('/nodes/2/export' in call.args[1] for call in self.client.request.call_args_list))
        second = backup(self.client, self.topology, self.root)
        self.assertNotEqual(result['directory'], second['directory'])

    def test_check_does_not_export_or_write(self):
        result = backup(self.client, self.topology, self.root, check=True)
        self.assertEqual(result['supported'][0]['node'], 'R0')
        self.assertIsNone(result['directory'])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertTrue(all(call.args[0] == 'GET' for call in self.client.request.call_args_list))

    def test_export_failure_does_not_download_old_config(self):
        self.export_error = EveAPIError('Export failed', 400)
        result = backup(self.client, self.topology, self.root)
        self.assertEqual(result['saved'], [])
        self.assertEqual(result['failed'][0]['node'], 'R0')
        self.assertFalse(any(call.args[1].endswith('/configs/1') for call in self.client.request.call_args_list))
        self.assertEqual(len(result['skipped']), 1)

    def test_empty_config_fails_and_remote_name_stays_inside_directory(self):
        self.config = ''
        self.assertEqual(len(backup(self.client, self.topology, self.root)['failed']), 1)
        self.config = 'hostname R0\n'
        self.nodes['1']['name'] = '../../outside'
        result = backup(self.client, self.topology, self.root)
        filename = result['saved'][0]['file']
        self.assertNotIn('/', filename)
        self.assertTrue((Path(result['directory']) / filename).is_file())

    def test_capability_api_failure_is_not_reported_as_unsupported(self):
        self.client.request.side_effect = EveAPIError('Unauthorized', 401)
        with self.assertRaises(EveAPIError):
            backup(self.client, self.topology, self.root)
        self.assertEqual(list(self.root.iterdir()), [])
