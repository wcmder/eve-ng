"""Restore saved node configurations using EVE-NG Community API."""
import json
from pathlib import Path
import sys
from urllib.parse import quote

from .deploy import indexed, lab_path, named


def read_backup(source):
    directory = Path(source).expanduser().resolve()
    manifest = json.loads((directory / 'manifest.json').read_text())
    if not isinstance(manifest, dict) or not isinstance(manifest.get('saved'), list):
        raise ValueError('Expected an eve backup manifest with a saved list')
    entries = []
    seen = set()
    for entry in manifest['saved']:
        if not isinstance(entry, dict) or any(not isinstance(entry.get(key), str) or not entry[key] for key in ('node', 'template', 'file')):
            raise ValueError('Invalid saved node entry in backup manifest')
        if entry['node'] in seen:
            raise ValueError('Duplicate node name in backup manifest: ' + entry['node'])
        seen.add(entry['node'])
        target = (directory / entry['file']).resolve()
        if not target.is_relative_to(directory) or not target.is_file():
            raise ValueError('Backup config must be a file inside its backup directory')
        config = target.read_text(encoding='utf-8')
        if not config.strip() or '\x00' in config:
            raise ValueError('Empty or binary config for ' + entry['node'])
        entries.append((entry, config))
    if not entries:
        raise ValueError('Backup contains no saved configurations')
    return directory, entries


def restore(client, topology, source, check=False, wipe=False):
    directory, entries = read_backup(source)
    path = lab_path(topology)
    nodes = named(client, path + '/nodes')
    supported = indexed(client.request('GET', path + '/configs'))
    result = {'lab': topology['name'], 'source': str(directory), 'check': check,
              'wipe': wipe, 'planned': [], 'skipped': [], 'completed': [], 'warnings': []}
    pending = []
    for entry, config in entries:
        name = entry['node']
        if name not in nodes:
            raise ValueError(f'Backup node {name} is missing from target lab; create matching nodes with eve apply first')
        node = nodes[name]
        if node['id'] not in supported:
            message = 'EVE-NG does not advertise startup-config support for this node'
            result['skipped'].append({'node': name, 'reason': message})
            print(f'Skipped {name}: {message}', file=sys.stderr)
            continue
        if node.get('template') != entry['template']:
            raise ValueError(f'Template mismatch for {name}: backup={entry["template"]}, remote={node.get("template")}')
        for key in ('image', 'ethernet'):
            if key in entry and str(entry[key]) != str(node.get(key)):
                raise ValueError(f'{key} mismatch for {name}: backup={entry[key]}, remote={node.get(key)}')
            if key not in entry:
                result['warnings'].append(f'{name}: older backup has no {key} metadata; compatibility must be checked manually')
        if not check and str(node.get('status')) != '0':
            raise RuntimeError(f'Stop {name} before restoring its configuration')
        result['planned'].append({'node': name, 'remote_id': node['id'], 'file': entry['file'],
                                  'stopped': str(node.get('status')) == '0',
                                  'actions': ['upload startup config', 'enable startup config'] + (['wipe writable node state'] if wipe else [])})
        pending.append((node, config))
    if check:
        return result

    def stopped(original):
        current = named(client, path + '/nodes').get(original['name'])
        if not current or current['id'] != original['id'] or str(current.get('status')) != '0':
            raise RuntimeError(f'Node {original["name"]} changed or started during restore')
        for key in ('template', 'image', 'ethernet'):
            if str(current.get(key)) != str(original.get(key)):
                raise RuntimeError(f'Node {original["name"]} {key} changed during restore')
        return current

    def verify_config(ident, config):
        data = client.request('GET', path + '/configs/' + quote(ident, safe=''))
        if not isinstance(data, dict) or str(data.get('id')) != ident or data.get('data') != config:
            raise RuntimeError('Server did not persist uploaded configuration for node ID ' + ident)

    try:
        # Complete and verify ALL uploads before any destructive wipe.
        for node, config in pending:
            stopped(node)
            ident = node['id']
            endpoint = path + '/nodes/' + quote(ident, safe='')
            client.request('PUT', path + '/configs/' + quote(ident, safe=''), {'id': ident, 'data': config})
            result['completed'].append('uploaded: ' + node['name'])
            verify_config(ident, config)
            stopped(node)
            client.request('PUT', endpoint, {'name': node['name'], 'config': '1'})
            result['completed'].append('enabled startup config: ' + node['name'])
            if str(stopped(node).get('config')) != '1':
                raise RuntimeError('Server did not enable startup config for ' + node['name'])
        if wipe:
            for node, config in pending:
                current = stopped(node)
                if str(current.get('config')) != '1':
                    raise RuntimeError('Startup config disabled before wipe for ' + node['name'])
                verify_config(node['id'], config)
                client.request('GET', path + '/nodes/' + quote(node['id'], safe='') + '/wipe')
                result['completed'].append('wiped: ' + node['name'])
                verify_config(node['id'], config)
                if str(stopped(node).get('config')) != '1':
                    raise RuntimeError('Startup config not enabled after wipe for ' + node['name'])
    except (RuntimeError, ValueError) as error:
        raise RuntimeError(f'Restore incomplete: {error}. Completed: {result["completed"]}. No rollback; inspect the lab before retrying') from error
    result['message'] = ('Startup configs prepared and selected nodes wiped; start nodes to import configs'
                         if wipe and pending else 'Startup configs staged; existing guest state is unchanged. Use --wipe to initialize nodes from the backup' if pending else 'No supported nodes to restore')
    return result
