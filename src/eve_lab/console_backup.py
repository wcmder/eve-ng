"""Back up running device configurations using our own authenticated consoles."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import paramiko

from .config import load_server
from .deploy import named, lab_path
from .device_console import Console, credentials
from .initialize import PaloConsole
from .palo_ssh import management_targets, connect_palo
from .session_discovery import login, DiscoveryError


def palo_running(console, timeout):
    console.command('set cli pager off')
    try:
        console.command('set cli op-command-xml-output on')
        output = console.command('show config running', timeout=timeout)
    finally:
        console.command('set cli op-command-xml-output off')
    # Exclude command echo/prompt; require a complete, parseable config tree.
    match = re.search(r'(<config(?:\s[^>]*)?>.*</config>)', output, re.S)
    if not match:
        raise RuntimeError('No complete XML configuration returned; backup not saved')
    try:
        config = ET.fromstring(match.group(1))
    except ET.ParseError:
        raise RuntimeError('Invalid or truncated XML configuration; backup not saved') from None
    if config.tag != 'config' or len(config) == 0:
        raise RuntimeError('Empty XML configuration; backup not saved')
    return match.group(1).strip() + '\n'


def backup(client, topology, root, server_name='default', check=False, node_name=None, timeout=600, management_ip=None):
    if not 1 <= timeout <= 3600:
        raise ValueError('--timeout must be between 1 and 3600 seconds')
    nodes = named(client, lab_path(topology) + '/nodes')
    if node_name:
        if node_name not in nodes:
            raise ValueError('Node not found: ' + node_name)
        nodes = {node_name: nodes[node_name]}
    targets = management_targets(root, topology['name'], node_name, management_ip)
    if management_ip and nodes[node_name].get('template') not in ('paloalto', 'panorama'):
        raise ValueError('--management-ip supports Palo Alto and Panorama only')
    result = {'lab': topology['name'], 'method': 'console', 'check': check, 'directory': None,
              'saved': [], 'supported': [], 'skipped': [], 'failed': []}
    pending = []
    for name, node in nodes.items():
        template = node.get('template')
        info = {'node': name, 'id': node['id'], 'template': template}
        info.update({key: node[key] for key in ('image', 'ethernet') if key in node})
        reason = None
        address = targets.get(name) if template in ('paloalto', 'panorama') else None
        url = urlsplit(node.get('url', ''))
        if template not in ('c8000v', 'paloalto', 'panorama'):
            reason = 'Unsupported device backup template'
        elif str(node.get('status')) != '2':
            reason = 'Start the node before backing up its running configuration'
        elif not address and (node.get('console') != 'telnet' or url.scheme != 'telnet' or not url.port):
            reason = 'Telnet console required; Palo/Panorama can instead use a management IP'
        if reason:
            result['skipped'].append({**info, 'reason': reason})
            print(f'Skipped {name}: {reason}', file=sys.stderr)
            continue
        info.update(transport='ssh' if address else 'telnet',
                    format='ios-running-config' if template == 'c8000v' else 'panos-running-xml')
        result['supported'].append(info)
        pending.append((info, address, url.port if not address else 22))
    if check or not pending:
        return result
    server = load_server(root, server_name, auth='ssh')
    auth = {info['template']: credentials(root, prefix='CISCO' if info['template'] == 'c8000v' else 'PALO')
            for info, _, _ in pending}
    directory = None
    ssh = paramiko.SSHClient()
    try:
        ssh.load_system_host_keys()
        ssh.connect(server.get('ssh_host') or urlsplit(server['url']).hostname,
                    username=server['ssh_username'], password=server['ssh_password'],
                    timeout=10, auth_timeout=10, banner_timeout=10, allow_agent=False, look_for_keys=False)
        for info, address, port in pending:
            name, template = info['node'], info['template']
            channel = device = None
            print(f'Backing up {name}...', file=sys.stderr, flush=True)
            try:
                user = auth[template]
                if address:
                    device, channel = connect_palo(ssh, address, user[0], user[1], timeout)
                else:
                    channel = ssh.get_transport().open_session(timeout=10)
                    channel.get_pty(term='vt100', width=512, height=1000)
                    channel.exec_command('telnet 127.0.0.1 ' + str(port))
                console = (Console if template == 'c8000v' else PaloConsole)(channel, boot_timeout=timeout)
                login(console, template, user)
                if template == 'c8000v':
                    console.command('terminal length 0')
                    config = console.command('more system:running-config', timeout=timeout)
                    config = re.sub(r'\A.*?Using \d+ out of \d+ bytes\n', '', config, flags=re.S)
                    if not re.search(r'^end\s*$', config, re.M):
                        raise RuntimeError('Incomplete running configuration; backup not saved')
                else:
                    config = palo_running(console, timeout)
                if directory is None:
                    base = Path(root) / 'labs' / topology['name'] / 'configs' / 'backups'
                    base.mkdir(parents=True, exist_ok=True)
                    directory = base / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
                    directory.mkdir(mode=0o700)
                    result['directory'] = str(directory.resolve())
                safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', name).strip('._')[:80] or 'node'
                safe_id = re.sub(r'[^A-Za-z0-9_-]', '_', info['id'])
                filename = safe_name + '-' + safe_id + ('.cfg' if template == 'c8000v' else '.xml')
                target = directory / filename
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                        stream.write(config)
                except OSError:
                    target.unlink(missing_ok=True)
                    raise
                result['saved'].append({**info, 'file': filename})
            except (RuntimeError, ValueError, OSError, paramiko.SSHException) as error:
                reason = str(error) if isinstance(error, DiscoveryError) else 'Config backup failed; check console readiness, credentials, complete config output, and local disk access (device output omitted)'
                result['failed'].append({**info, 'reason': reason})
                print(f'Failed {name}: {reason}', file=sys.stderr)
            finally:
                if channel is not None:
                    channel.close()
                if device is not None:
                    device.close()
    finally:
        ssh.close()
        if directory is not None:
            with os.fdopen(os.open(directory / 'manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
                json.dump(result, stream, indent=2)
                stream.write('\n')
    return result
