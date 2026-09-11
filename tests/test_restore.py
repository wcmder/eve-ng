import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from eve_lab.restore import restore


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.entries = [{'node': name, 'id': old, 'template': 'c8000v',
                         'image': 'c8000v-test', 'ethernet': 4, 'file': name + '.cfg'}
                        for name, old in [('R0', '1'), ('R-A', '2')]]
        for entry in self.entries:
            (self.directory / entry['file']).write_text('hostname ' + entry['node'] + '\n')
        self.manifest()
        self.nodes = {ident: {'name': entry['node'], 'template': entry['template'],
                             'image': entry['image'], 'ethernet': 4, 'status': 0, 'config': '0'}
                      for ident, entry in zip(('7', '8'), self.entries)}
        self.supported = self.nodes.copy()
        self.configs = {}
        self.fail_upload = None
        self.corrupt = False
        self.client = MagicMock()
        self.client.request.side_effect = self.request
        self.topology = {'name': 'palo-lab'}
        self.stderr = patch('sys.stderr').start()
        self.addCleanup(patch.stopall)

    def manifest(self):
        (self.directory / 'manifest.json').write_text(json.dumps({'saved': self.entries}))

    def request(self, method, path, data=None):
        if path.endswith('/nodes'): return self.nodes
        if path.endswith('/configs'): return self.supported
        ident = path.split('/')[-1]
        if '/configs/' in path:
            if method == 'PUT':
                if ident == self.fail_upload: raise RuntimeError('upload failed')
                self.configs[ident] = data['data']
                return None
            return {'id': int(ident), 'data': 'wrong' if self.corrupt else self.configs[ident]}
        if path.endswith('/wipe'):
            self.assertEqual(method, 'GET')
            return None
        if method == 'PUT':
            self.nodes[ident].update(data)
            return None
        raise AssertionError((method, path, data))

    def run_restore(self, **kwargs):
        return restore(self.client, self.topology, self.directory, **kwargs)

    def mutations(self):
        return [call for call in self.client.request.call_args_list
                if call.args[0] != 'GET' or call.args[1].endswith('/wipe')]

    def test_stage_maps_names_to_new_ids_without_wipe(self):
        result = self.run_restore()
        self.assertEqual(self.configs['7'], 'hostname R0\n')
        self.assertEqual(self.nodes['8']['config'], '1')
        self.assertEqual(result['planned'][0]['remote_id'], '7')
        self.assertFalse(any(c.args[1].endswith('/wipe') for c in self.mutations()))

    def test_preview_running_nodes_never_writes_or_wipes(self):
        self.nodes['7']['status'] = 2
        result = self.run_restore(check=True, wipe=True)
        self.assertFalse(result['planned'][0]['stopped'])
        self.assertEqual(self.mutations(), [])

    def test_all_nodes_preflight_before_writes(self):
        for key, value in [('status', 2), ('template', 'paloalto'), ('ethernet', 8), ('image', 'different')]:
            with self.subTest(key=key):
                old = self.nodes['8'][key]
                self.nodes['8'][key] = value
                with self.assertRaises((ValueError, RuntimeError)):
                    self.run_restore(wipe=True)
                self.assertEqual(self.mutations(), [])
                self.nodes['8'][key] = old

    def test_wipes_only_after_all_uploads(self):
        self.run_restore(wipe=True)
        calls = self.client.request.call_args_list
        uploads = [i for i, c in enumerate(calls) if c.args[0] == 'PUT' and '/configs/' in c.args[1]]
        wipes = [i for i, c in enumerate(calls) if c.args[1].endswith('/wipe')]
        self.assertEqual(len(wipes), 2)
        self.assertLess(max(uploads), min(wipes))

    def test_failed_or_unverified_upload_prevents_wipe(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                self.client.reset_mock()
                self.corrupt = corrupt
                self.fail_upload = '8' if not corrupt else None
                with self.assertRaisesRegex(RuntimeError, 'Restore incomplete'):
                    self.run_restore(wipe=True)
                self.assertFalse(any(c.args[1].endswith('/wipe') for c in self.mutations()))

    def test_unsupported_skipped(self):
        del self.supported['8']
        result = self.run_restore(wipe=True)
        self.assertEqual(result['skipped'][0]['node'], 'R-A')
        self.assertNotIn('8', self.configs)

    def test_missing_node_prevents_writes(self):
        del self.nodes['8']
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.run_restore()
        self.assertEqual(self.mutations(), [])

    def test_bad_files_rejected_before_api_access(self):
        for filename in ('../outside.cfg', '/etc/hosts'):
            self.entries[0]['file'] = filename
            self.manifest()
            with self.assertRaises(ValueError): self.run_restore()
            self.client.request.assert_not_called()

    def test_node_starting_during_restore_prevents_upload(self):
        original = self.request
        count = 0
        def request(method, path, data=None):
            nonlocal count
            if path.endswith('/nodes'):
                count += 1
                if count == 2: self.nodes['7']['status'] = 2
            return original(method, path, data)
        self.client.request.side_effect = request
        with self.assertRaisesRegex(RuntimeError, 'changed or started'):
            self.run_restore(wipe=True)
        self.assertEqual(self.mutations(), [])
