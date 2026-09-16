"""Validate locally saved backup manifests."""
import json
from pathlib import Path



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
