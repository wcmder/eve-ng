"""Back up exportable node configurations through the EVE-NG API."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote

from .deploy import indexed, lab_path


def backup(client, topology, root, check=False):
    path = lab_path(topology)
    nodes = indexed(client.request('GET', path + '/nodes'))
    # Community API lists nodes whose templates support startup-config export,
    # including nodes without an existing export. An API failure is not an empty list.
    supported = indexed(client.request('GET', path + '/configs'))
    result = {'lab': topology['name'], 'check': check, 'directory': None,
              'saved': [], 'supported': [], 'skipped': [], 'failed': []}
    directory = None
    for ident, node in nodes.items():
        name = node['name']
        info = {'node': name, 'id': ident, 'template': node.get('template')}
        info.update({key: node[key] for key in ('image', 'ethernet') if key in node})
        if ident not in supported:
            message = 'EVE-NG does not advertise configuration export support for this node/template'
            result['skipped'].append({**info, 'reason': message})
            print(f"Skipped {name} ({node.get('template', 'unknown')}): {message}", file=sys.stderr)
            continue
        result['supported'].append(info)
        if check:
            continue
        # Do not read an old stored configuration when a fresh export fails.
        print(f"Exporting {name}...", file=sys.stderr, flush=True)
        try:
            client.request('PUT', path + '/nodes/' + quote(ident, safe='') + '/export')
            data = client.request('GET', path + '/configs/' + quote(ident, safe=''))
            if not isinstance(data, dict) or str(data.get('id')) != ident:
                raise RuntimeError('Unexpected configuration response or node ID')
            config = data.get('data')
            if not isinstance(config, str) or not config.strip():
                raise RuntimeError('Export returned no configuration; save the guest configuration and retry')
            if directory is None:
                directory = Path(root) / 'labs' / topology['name'] / 'configs' / 'backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
                directory.mkdir(parents=True, mode=0o700)
                result['directory'] = str(directory.resolve())
            # Include node ID to avoid collisions and sanitize untrusted remote names.
            safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', name).strip('._')[:80] or 'node'
            safe_id = re.sub(r'[^A-Za-z0-9_-]', '_', ident)
            filename = f'{safe_name}-{safe_id}.cfg'
            target = directory / filename
            with target.open('x', encoding='utf-8') as stream:
                target.chmod(0o600)
                stream.write(config)
            result['saved'].append({**info, 'file': filename})
        except (RuntimeError, OSError, ValueError) as error:
            result['failed'].append({**info, 'reason': str(error)})
            print(f"Failed {name}: {error}", file=sys.stderr)
    if directory is not None:
        manifest = directory / 'manifest.json'
        with manifest.open('x', encoding='utf-8') as stream:
            manifest.chmod(0o600)
            json.dump(result, stream, indent=2)
            stream.write('\n')
    return result
