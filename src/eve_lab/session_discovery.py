"""Read initialized devices through EVE serial consoles for SSH session discovery."""
from ipaddress import IPv4Address
import re
from urllib.parse import urlsplit

import paramiko

from .deploy import named, lab_path, interfaces
from .device_console import Console, credentials
from .initialize import PaloConsole
from .topology import interface_key


class DiscoveryError(RuntimeError):
    """A safe diagnostic that contains no device output or credentials."""


def login(console, template, auth):
    """Authenticate without answering setup or password-change prompts."""
    console.send('')
    seen = set()
    enabling = False
    pattern = (r'(?im:^[^\n]*login:\s*$|^username:\s*$|^password:\s*$|'
               r'^.*(?:new password|old password|confirm password|enable secret|initial configuration|your selection|login incorrect|authentication failed).*$)|'
               r'^[\w.@()/:-]+[>#]\s*$')
    for _ in range(8):
        _, match = console.expect(pattern, timeout=console.boot_timeout)
        prompt = match.group().strip()
        lower = prompt.lower()
        if re.search('new password|old password|confirm password|enable secret|initial configuration|your selection|login incorrect|authentication failed', lower):
            raise DiscoveryError('Device needs initialization or rejected configured credentials')
        if prompt.endswith(('>', '#')):
            if '(config' in lower or (template != 'c8000v' and prompt.endswith('#')):
                raise DiscoveryError('Console is in configuration mode; exit it before discovery')
            if template == 'c8000v' and prompt.endswith('>'):
                if enabling:
                    raise DiscoveryError('Enable authentication failed')
                enabling = True
                console.send('enable')
                continue
            console.prompt = prompt
            return
        stage = 'enable' if enabling else ('username' if lower.endswith(('login:', 'username:')) else 'password')
        if stage in seen:
            raise DiscoveryError('Console authentication failed; not retrying credentials')
        seen.add(stage)
        console.send(auth[2] if stage == 'enable' else auth[0] if stage == 'username' else auth[1])
    raise DiscoveryError('Console login did not complete')


def palo_identity(output):
    values = dict(re.findall(r'^\s*(hostname|ip-address):\s*(\S+)\s*$', output, re.M))
    if not values.get('hostname') or not values.get('ip-address'):
        raise DiscoveryError('Management hostname/IP missing from system information')
    address = IPv4Address(values['ip-address'])
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        raise DiscoveryError('Management IP is not a usable SSH target')
    return values['hostname'], str(address)


def discover(client, topology, root, server, timeout=60):
    path = lab_path(topology)
    nodes = named(client, path + '/nodes')
    networks = named(client, path + '/networks')
    management = {item['id'] for item in networks.values() if item.get('type') == 'pnet1'}
    result = {'leases': [], 'skipped': []}
    ssh = paramiko.SSHClient()
    try:
        ssh.load_system_host_keys()
        ssh.connect(server.get('ssh_host') or urlsplit(server['url']).hostname,
                    username=server['ssh_username'], password=server['ssh_password'],
                    timeout=10, auth_timeout=10, banner_timeout=10, allow_agent=False, look_for_keys=False)
        for name, node in nodes.items():
            channel = None
            stage = 'checking node settings'
            try:
                template = node.get('template')
                if template not in ('c8000v', 'paloalto', 'panorama'):
                    raise DiscoveryError('Unsupported console discovery template')
                if str(node.get('status')) != '2':
                    raise DiscoveryError('Node is not running')
                url = urlsplit(node.get('url', ''))
                if node.get('console') != 'telnet' or url.scheme != 'telnet' or not url.port:
                    raise DiscoveryError('Working Telnet console required')
                stage = 'loading device credentials'
                auth = credentials(root, prefix='CISCO' if template == 'c8000v' else 'PALO')
                stage = 'opening Telnet console'
                channel = ssh.get_transport().open_session(timeout=10)
                channel.get_pty(term='vt100', width=512, height=1000)
                channel.exec_command('telnet 127.0.0.1 ' + str(url.port))
                console = (Console if template == 'c8000v' else PaloConsole)(channel, boot_timeout=timeout)
                stage = 'logging into console'
                login(console, template, auth)
                stage = 'reading hostname and management IP'
                if template == 'c8000v':
                    connected = {interface_key(port['name']) for port in interfaces(client, path, node).values()
                                 if str(port.get('network_id')) in management}
                    records = console.interface_status()
                    hostname = console.prompt.rstrip('#>')
                    addresses = [item['ip_address'] for item in records
                                 if interface_key(item['interface']) in connected
                                 and item['status'] == 'up' and item['protocol'] == 'up']
                    if not addresses:
                        raise DiscoveryError('No active IPv4 address on a pnet1-connected interface')
                else:
                    console.command('set cli pager off')
                    hostname, address = palo_identity(console.command('show system info'))
                    addresses = [address]
                result['leases'].extend({'hostname': hostname, 'ip_address': address, 'status': 'active'} for address in addresses)
            except (RuntimeError, ValueError, OSError, paramiko.SSHException) as error:
                # Only our own fixed diagnostics are safe to expose.
                reason = str(error) if isinstance(error, DiscoveryError) else (
                    'Timed out waiting for console prompt' if str(error).startswith('Timed out waiting for console prompt')
                    else 'Console or credential operation failed; inspect the console and configured credentials')
                result['skipped'].append({'node': name, 'stage': stage, 'reason': reason})
            finally:
                if channel is not None:
                    channel.close()
    finally:
        ssh.close()
    return result
