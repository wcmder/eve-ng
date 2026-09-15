"""Prepare Palo bootstrap media and optionally attach it to one stopped node."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
from urllib.parse import quote, urlsplit
import xml.etree.ElementTree as ET

import paramiko

from .config import load_server
from .deploy import lab_path, named
from .device_console import credentials

# EVE bind-mounts this tree into each QEMU jail. An arbitrary /opt/unetlab
# directory exists on the host but is invisible inside the VM runtime.
REMOTE_BOOTSTRAP_ROOT = '/opt/unetlab/addons/qemu/.eve-bootstrap'


def without_managed_cdrom(options):
    pattern = (r' -drive file=/opt/unetlab/(?:bootstrap|addons/qemu/\.eve-bootstrap)/'
               r'[a-f0-9]{24}/[0-9]{8}T[0-9]{12}Z/cdrom\.iso,media=cdrom,if=ide,(?:index=2,)?readonly=on(?=\s|$)')
    base, count = re.subn(pattern, '', options)
    if count > 1 or '-cdrom' in base or 'media=cdrom' in base:
        raise ValueError('Node has unrecognized CD-ROM options; inspect before changing bootstrap media')
    return base


def bootstrap_files(hostname, username, password):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', hostname):
        raise ValueError('Invalid bootstrap hostname')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', username):
        raise ValueError('Bootstrap username must contain only letters, digits, dots, underscores or hyphens')
    if not password or any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise ValueError('Bootstrap password must be nonempty without control characters')
    # PAN bootstrap phash uses crypt format; plaintext is passed on stdin only.
    process = subprocess.run(['openssl', 'passwd', '-1', '-stdin'], input=password + '\n',
                             text=True, capture_output=True, check=False)
    phash = process.stdout.strip()
    if process.returncode or not re.fullmatch(r'\$1\$[./A-Za-z0-9]{1,8}\$[./A-Za-z0-9]{22}', phash):
        raise RuntimeError('Unable to generate Palo bootstrap password hash with openssl')
    root = ET.Element('config', version='11.2.0', urldb='paloaltonetworks')
    users = ET.SubElement(ET.SubElement(root, 'mgt-config'), 'users')
    admin = ET.SubElement(users, 'entry', name=username)
    ET.SubElement(admin, 'phash').text = phash
    ET.SubElement(ET.SubElement(ET.SubElement(admin, 'permissions'), 'role-based'), 'superuser').text = 'yes'
    ET.SubElement(root, 'shared')
    device = ET.SubElement(ET.SubElement(root, 'devices'), 'entry', name='localhost.localdomain')
    network = ET.SubElement(device, 'network')
    ET.SubElement(ET.SubElement(network, 'interface'), 'ethernet')
    ET.SubElement(ET.SubElement(network, 'virtual-router'), 'entry', name='default')
    system = ET.SubElement(ET.SubElement(device, 'deviceconfig'), 'system')
    ET.SubElement(system, 'hostname').text = hostname
    dhcp = ET.SubElement(ET.SubElement(system, 'type'), 'dhcp-client')
    for key, value in [('send-hostname', 'yes'), ('send-client-id', 'no'),
                       ('accept-dhcp-hostname', 'no'), ('accept-dhcp-domain', 'no')]:
        ET.SubElement(dhcp, key).text = value
    services = ET.SubElement(system, 'service')
    for key, value in [('disable-ssh', 'no'), ('disable-https', 'no'),
                       ('disable-telnet', 'yes'), ('disable-http', 'yes')]:
        ET.SubElement(services, key).text = value
    ET.SubElement(ET.SubElement(device, 'vsys'), 'entry', name='vsys1')
    ET.indent(root)
    xml = ET.tostring(root, encoding='unicode', xml_declaration=True) + '\n'
    init = ('type=dhcp-client\nhostname=' + hostname + '\n'
            'dhcp-send-hostname=yes\ndhcp-send-client-id=no\n'
            'dhcp-accept-server-hostname=no\ndhcp-accept-server-domain=no\n')
    return {'config/init-cfg.txt': init, 'config/bootstrap.xml': xml}


def prepare(client, topology, root, server_name, node_name, check=False, attach=False):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', node_name):
        raise ValueError('Invalid bootstrap node name')
    path = lab_path(topology)
    node = named(client, path + '/nodes').get(node_name)
    if node and node.get('template') == 'panorama':
        raise ValueError(
            'Panorama configuration bootstrap is not supported by this command. '
            'The inspected Panorama 12.1.5 image restricts ISO bootstrap to external '
            'software installation; it rejects the firewall init-cfg.txt/bootstrap.xml '
            'workflow. Configure administrator credentials and management networking '
            'through the Panorama console, then commit. No media was generated or attached.'
        )
    if not node or node.get('template') != 'paloalto':
        raise ValueError('Select an existing Palo Alto node with --node')
    endpoint = path + '/nodes/' + quote(node['id'], safe='')
    detail = client.request('GET', endpoint)
    if not str(detail.get('image', '')).startswith('paloalto-11.2.'):
        raise ValueError('Bootstrap test currently targets Palo Alto 11.2 images only')
    options = detail.get('qemu_options')
    if not isinstance(options, str) or not options.strip():
        raise ValueError('Server did not return existing QEMU options')
    base_options = without_managed_cdrom(options)
    if attach and str(detail.get('status')) != '0':
        raise ValueError('Stop ' + node_name + ' before attaching bootstrap media')
    result = {'lab': topology['name'], 'node': node_name, 'check': check,
              'attached': False, 'stopped': str(detail.get('status')) == '0',
              'note': 'Bootstrap requires factory-default first boot; this command never wipes or starts the node'}
    if check:
        return result
    username, password = credentials(root, prefix='PALO')
    files = bootstrap_files(node_name, username, password)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    local = Path(root) / '.state' / 'bootstrap' / topology['name'] / node_name / stamp
    local.mkdir(parents=True, mode=0o700)
    for folder in ('config', 'content', 'software', 'license'):
        (local / folder).mkdir(mode=0o700)
    for filename, content in files.items():
        target = local / filename
        with target.open('x') as stream:
            target.chmod(0o600)
            stream.write(content)
    server = load_server(root, server_name, auth='ssh')
    scope = hashlib.sha256((server['url'] + '/' + path + '/' + node['id']).encode()).hexdigest()[:24]
    remote = REMOTE_BOOTSTRAP_ROOT + '/' + scope + '/' + stamp
    iso = remote + '/cdrom.iso'
    # Use QEMU's default CD-ROM slot. Without index=2 QEMU also creates an
    # empty IDE CD-ROM; PAN-OS mounts only /dev/cdrom during media detection.
    updated = base_options + ' -drive file=' + iso + ',media=cdrom,if=ide,index=2,readonly=on'
    result.update({'directory': str(local.resolve()), 'remote_iso': iso,
                   'previous_qemu_options': options, 'proposed_qemu_options': updated})
    manifest = local / 'manifest.json'
    def save_manifest():
        with manifest.open('w') as stream:
            manifest.chmod(0o600)
            json.dump(result, stream, indent=2)
            stream.write('\n')
    save_manifest()
    ssh = paramiko.SSHClient()
    try:
        ssh.load_system_host_keys()
        ssh.connect(server.get('ssh_host') or urlsplit(server['url']).hostname,
                    username=server['ssh_username'], password=server['ssh_password'],
                    timeout=10, auth_timeout=10, banner_timeout=10, allow_agent=False, look_for_keys=False)
        with ssh.open_sftp() as sftp:
            for directory in [REMOTE_BOOTSTRAP_ROOT, REMOTE_BOOTSTRAP_ROOT + '/' + scope]:
                try: sftp.stat(directory)
                except FileNotFoundError: sftp.mkdir(directory, mode=0o700)
            sftp.mkdir(remote, mode=0o700)
            sftp.mkdir(remote + '/package', mode=0o700)
            for folder in ('config', 'content', 'software', 'license'):
                sftp.mkdir(remote + '/package/' + folder, mode=0o700)
            for filename in files:
                target = remote + '/package/' + filename
                sftp.put(str(local / filename), target)
                sftp.chmod(target, 0o600)
            command = ('umask 077 && genisoimage -quiet -iso-level 3 -J -R -V bootstrap -o ' +
                       shlex.quote(iso) + ' ' + shlex.quote(remote + '/package'))
            stdin, stdout, stderr = ssh.exec_command(command, timeout=60)
            stdin.close()
            stdout.read()
            stderr.read()
            if stdout.channel.recv_exit_status():
                raise RuntimeError('Bootstrap ISO build failed; inspect genisoimage on the EVE host')
            iso_size = sftp.stat(iso).st_size
            if iso_size < 32768:
                raise RuntimeError('Bootstrap ISO is unexpectedly small')
            sftp.get(iso, str(local / 'cdrom.iso'))
            (local / 'cdrom.iso').chmod(0o600)
            result['iso_bytes'] = iso_size
        if attach:
            current = client.request('GET', endpoint)
            for field in ('name', 'image', 'qemu_options'):
                if current.get(field) != detail.get(field):
                    raise RuntimeError('Node changed during preparation; ISO prepared but not attached')
            if str(current.get('status')) != '0':
                raise RuntimeError('Node started during preparation; ISO prepared but not attached')
            client.request('PUT', endpoint, {'name': node_name, 'qemu_options': updated, 'ro_qemu_options': options})
            persisted = client.request('GET', endpoint)
            if persisted.get('qemu_options') != updated:
                raise RuntimeError('EVE did not persist bootstrap attachment; inspect node QEMU options')
            result['attached'] = True
        save_manifest()
    except (paramiko.SSHException, OSError):
        raise RuntimeError('Bootstrap SSH/file operation failed; inspect .state/bootstrap manifest and EVE SSH connectivity') from None
    finally:
        ssh.close()
    return result
