"""Restore saved running configurations through authenticated device consoles."""
import base64
import hashlib
from ipaddress import IPv4Address
import json
import re
import sys
from urllib.parse import urlsplit
from uuid import uuid4
import xml.etree.ElementTree as ET

import paramiko

from .config import load_server
from .deploy import named, lab_path
from .device_console import Console, credentials
from .initialize import PaloConsole
from .palo_ssh import connect_palo, management_targets
from .restore import read_backup
from .session_discovery import login


class RestoreError(RuntimeError):
    """Safe fixed diagnostic, without device output."""


def validate_config(entry, config):
    if any(ord(c) < 32 and c not in '\n\r\t' for c in config):
        raise ValueError('Control characters in backup configuration')
    template = entry['template']
    if template == 'c8000v':
        if entry.get('format') not in (None, 'ios-running-config'):
            raise ValueError('Unsupported Cisco backup format')
        if not re.search(r'^version \S+', config, re.M) or not re.search(r'^end\s*\Z', config, re.M):
            raise ValueError('Cisco restore requires a complete running configuration ending in end')
    elif template in ('paloalto', 'panorama'):
        if entry.get('format') not in (None, 'panos-running-xml'):
            raise ValueError('Unsupported PAN-OS backup format')
        try:
            if '<!DOCTYPE' in config or '<!ENTITY' in config:
                raise ValueError('Unsupported XML declarations')
            tree = ET.fromstring(config)
            if tree.tag != 'config' or not len(tree):
                raise ValueError('Missing config tree')
        except (ET.ParseError, ValueError):
            raise ValueError('PAN-OS restore requires a complete config XML file') from None


def host_details(ssh, address=None):
    if address:
        host = str(IPv4Address(address))
    else:
        stdin, stdout, stderr = ssh.exec_command('ip -j -4 address show dev pnet1', timeout=15)
        stdin.close()
        info = json.loads(stdout.read())
        if stdout.channel.recv_exit_status() != 0:
            raise RestoreError('Cannot read EVE pnet1 address; specify --transfer-host')
        addresses = [item['local'] for link in info for item in link.get('addr_info', []) if item.get('scope') == 'global']
        if len(addresses) != 1:
            raise RestoreError('EVE pnet1 must have one IPv4 address; specify --transfer-host')
        host = str(IPv4Address(addresses[0]))
    return host


def trusted_fingerprints(sftp):
    result = set()
    # Read public keys over the already host-key-verified EVE SSH connection.
    for name in sftp.listdir('/etc/ssh'):
        if not re.fullmatch(r'ssh_host_[a-z0-9]+_key.pub', name):
            continue
        with sftp.open('/etc/ssh/' + name, 'r') as stream:
            parts = stream.read().split()
        key = base64.b64decode(parts[1], validate=True)
        result.add('SHA256:' + base64.b64encode(hashlib.sha256(key).digest()).decode().rstrip('='))
        result.add(':'.join(f'{byte:02x}' for byte in hashlib.md5(key).digest()))
    if not result:
        raise RestoreError('No EVE SSH public keys available to verify device SCP trust')
    return result


def import_file(console, template, user, host, path, filename, password, fingerprints, timeout):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', user):
        raise RestoreError('EVE SSH username is not valid for device SCP')
    if not password or any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise RestoreError('EVE SSH password contains unsupported console characters')
    command = (f'copy scp://{user}@{host}/{path.lstrip("/")} bootflash:{filename}' if template == 'c8000v'
               else f'scp import configuration from {user}@{host}:{path}')
    console.send(command)
    seen = set()
    transcript = ''
    pattern = (r'(?im:^[^\n]*password:\s*$|^Destination filename[^\n]*\?\s*$|'
               r'^.*(?:continue connecting|accept this certificate|accept this key)[^\n]*[?]\s*(?:\([^\n]*\))?\s*$)|'
               r'^[\w.@()/:-]+[>#]\s*$')
    for _ in range(6):
        output, match = console.expect(pattern, timeout=timeout)
        transcript += output
        prompt = match.group().strip()
        if prompt.endswith(('>', '#')):
            if re.search(r'(?im)permission denied|host key verification failed|connection refused|no such file|error|failed', transcript):
                raise RestoreError('SCP import failed; check device-to-EVE routing, SSH algorithms and credentials')
            marker = r'\d+ bytes copied' if template == 'c8000v' else r'100%|configuration imported successfully'
            if not re.search(marker, transcript, re.I):
                raise RestoreError('SCP transfer completion was not confirmed')
            return
        lower = prompt.lower()
        stage = 'password' if 'password:' in lower else 'filename' if lower.startswith('destination filename') else 'hostkey'
        if stage in seen:
            raise RestoreError('Repeated SCP prompt; refusing repeated authentication')
        seen.add(stage)
        if stage == 'hostkey':
            reported = set(re.findall(r'SHA256:[A-Za-z0-9+/]+|(?:[0-9a-fA-F]{2}:){15}[0-9a-fA-F]{2}', transcript))
            if not reported.intersection(fingerprints):
                console.send('no')
                raise RestoreError('Device SCP host fingerprint does not match the trusted EVE host')
            console.send('yes')
        else:
            console.send(password if stage == 'password' else '')
    raise RestoreError('SCP prompt sequence did not complete')


def apply_file(console, template, filename, config, timeout):
    if template == 'c8000v':
        digest = hashlib.md5(config.encode('utf-8')).hexdigest()
        output = console.command(f'verify /md5 bootflash:{filename}', timeout=timeout)
        if digest not in output.lower():
            raise RestoreError('Cisco staged file checksum does not match backup')
        output = console.command(f'configure replace bootflash:{filename} force', timeout=timeout)
        if 'Rollback Done' not in output or re.search(r'(?i)rollback failed|error|failed to', output):
            raise RestoreError('Cisco configuration replacement not confirmed; partial changes may remain')
        output = console.command('write memory', timeout=timeout)
        if '[OK]' not in output:
            raise RestoreError('Cisco save not confirmed')
    else:
        console.command('configure')
        output = console.command(f'load config from {filename}', timeout=timeout)
        if not re.search(r'(?i)configuration loaded successfully', output):
            raise RestoreError('PAN-OS config load not confirmed; not committing')
        output = console.command('commit', timeout=timeout)
        if not re.search(r'Configuration committed successfully|There are no changes to commit', output, re.I):
            raise RestoreError('PAN-OS commit not confirmed; inspect candidate configuration and jobs')
        console.command('exit')


def restore(client, topology, source, root, server_name='default', check=False, node_name=None,
            timeout=600, management_ip=None, transfer_host=None):
    if not 1 <= timeout <= 3600:
        raise ValueError('--timeout must be between 1 and 3600 seconds')
    if transfer_host:
        transfer_host = str(IPv4Address(transfer_host))
    directory, entries = read_backup(source)
    if node_name:
        entries = [(entry, config) for entry, config in entries if entry['node'] == node_name]
        if not entries:
            raise ValueError('Node not found in backup: ' + node_name)
    nodes = named(client, lab_path(topology) + '/nodes')
    targets = management_targets(root, topology['name'], node_name, management_ip)
    result = {'lab': topology['name'], 'source': str(directory), 'check': check, 'planned': [],
              'completed': [], 'failed': [], 'skipped': [], 'warnings': []}
    pending = []
    for entry, config in entries:
        name, template = entry['node'], entry['template']
        if template not in ('c8000v', 'paloalto', 'panorama'):
            result['skipped'].append({'node': name, 'reason': 'Unsupported restore template'})
            continue
        validate_config(entry, config)
        if name not in nodes:
            raise ValueError('Backup node missing from lab: ' + name)
        node = nodes[name]
        for key in ('template', 'image', 'ethernet'):
            if key in entry and str(entry[key]) != str(node.get(key)):
                raise ValueError(f'{name}: {key} does not match backup')
            if key not in entry:
                result['warnings'].append({'node': name, 'reason': 'Backup has no ' + key + ' metadata'})
        address = targets.get(name) if template != 'c8000v' else None
        if management_ip and template == 'c8000v':
            raise ValueError('--management-ip supports Palo/Panorama only')
        url = urlsplit(node.get('url', ''))
        if not address and (node.get('console') != 'telnet' or url.scheme != 'telnet' or not url.port):
            raise ValueError('Working Telnet console or Palo management IP required for ' + name)
        if not check and str(node.get('status')) != '2':
            raise ValueError('Start ' + name + ' before restoring')
        result['planned'].append({'node': name, 'file': entry['file'], 'transport': 'ssh' if address else 'telnet',
                                  'running': str(node.get('status')) == '2',
                                  'actions': ['SCP import from EVE host', 'replace running config', 'save'] if template == 'c8000v'
                                  else ['SCP import from EVE host', 'load candidate config', 'commit']})
        pending.append((entry, config, node, address, url.port if not address else 22))
    if check or not pending:
        return result
    server = load_server(root, server_name, auth='ssh')
    auth = {entry['template']: credentials(root, prefix='CISCO' if entry['template'] == 'c8000v' else 'PALO')
            for entry, *_ in pending}
    ssh = paramiko.SSHClient()
    sftp = None
    stage_dir = None
    staged = []
    try:
        ssh.load_system_host_keys()
        ssh.connect(server.get('ssh_host') or urlsplit(server['url']).hostname,
                    username=server['ssh_username'], password=server['ssh_password'],
                    timeout=10, auth_timeout=10, banner_timeout=10, allow_agent=False, look_for_keys=False)
        host = host_details(ssh, transfer_host)
        sftp = ssh.open_sftp()
        fingerprints = trusted_fingerprints(sftp)
        stage_dir = '/tmp/eve-restore-' + uuid4().hex
        sftp.mkdir(stage_dir, mode=0o700)
        for entry, config, original, address, port in pending:
            channel = device = None
            name, template = entry['node'], entry['template']
            filename = 'eve-restore-' + uuid4().hex + ('.cfg' if template == 'c8000v' else '.xml')
            remote_path = stage_dir + '/' + filename
            print('Restoring ' + name + '...', file=sys.stderr, flush=True)
            try:
                current = named(client, lab_path(topology) + '/nodes').get(name)
                if not current or any(str(current.get(k)) != str(original.get(k)) for k in ('id', 'template', 'image', 'ethernet', 'console', 'url')) or str(current.get('status')) != '2':
                    raise RestoreError('Target changed or stopped during restore')
                staged.append(remote_path)
                with sftp.open(remote_path, 'w') as stream:
                    sftp.chmod(remote_path, 0o600)
                    stream.write(config.encode('utf-8'))
                with sftp.open(remote_path, 'r') as stream:
                    if stream.read() != config.encode('utf-8'):
                        raise RestoreError('EVE staging file verification failed')
                user = auth[template]
                if address:
                    device, channel = connect_palo(ssh, address, user[0], user[1], timeout)
                else:
                    channel = ssh.get_transport().open_session(timeout=10)
                    channel.get_pty(term='vt100', width=512, height=1000)
                    channel.exec_command('telnet 127.0.0.1 ' + str(port))
                console = (Console if template == 'c8000v' else PaloConsole)(channel, boot_timeout=timeout)
                login(console, template, user)
                console.command('terminal length 0' if template == 'c8000v' else 'set cli pager off')
                import_file(console, template, server['ssh_username'], host, remote_path, filename,
                            server['ssh_password'], fingerprints, timeout)
                apply_file(console, template, filename, config, timeout)
                result['completed'].append(name)
            except (RuntimeError, ValueError, OSError, paramiko.SSHException) as error:
                reason = str(error) if isinstance(error, RestoreError) else 'Device restore failed; inspect console, credentials and SCP connectivity (device output omitted)'
                result['failed'].append({'node': name, 'reason': reason, 'device_file': filename})
                print('Failed ' + name + ': ' + reason + '; partial changes may remain', file=sys.stderr)
            finally:
                if channel is not None: channel.close()
                if device is not None: device.close()
    finally:
        if sftp is not None:
            for path in staged:
                try: sftp.remove(path)
                except OSError: result['warnings'].append({'reason': 'Could not remove EVE staging file', 'path': path})
            if stage_dir:
                try: sftp.rmdir(stage_dir)
                except OSError: result['warnings'].append({'reason': 'Could not remove EVE staging directory', 'path': stage_dir})
            sftp.close()
        ssh.close()
    return result
