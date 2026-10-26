"""Read initialized devices through EVE serial consoles for SSH session discovery."""
from ipaddress import IPv4Address
import re
from urllib.parse import urlsplit

import paramiko

from .deploy import named, lab_path, interfaces
from .device_console import Console, credentials
from .initialize import PaloConsole
from .topology import interface_key
from .versa_console import VERSA_PREFIXES, SHELL, CLI, LOGIN, PASSWORD, FAILURE, WIZARD


class DiscoveryError(RuntimeError):
    """A safe diagnostic that contains no device output or credentials."""


def login(console, template, auth):
    """Authenticate without answering setup or password-change prompts."""
    console.send('')
    seen = set()
    enabling = False
    stale_enable_prompts = 0
    pattern = (r'(?im:^[^\n]*login:\s*$|^username:\s*$|^password:\s*$|'
               r'^.*(?:new password|old password|confirm password|enable secret|initial configuration|your selection|login incorrect|authentication failed|access denied|bad secrets).*$)|'
               r'^[\w.@()/:-]+[>#]\s*$')
    for _ in range(8):
        _, match = console.expect(pattern, timeout=console.boot_timeout)
        prompt = match.group().strip()
        lower = prompt.lower()
        if re.search('new password|old password|confirm password|enable secret|initial configuration|your selection|login incorrect|authentication failed|access denied|bad secrets', lower):
            raise DiscoveryError('Device needs initialization or rejected configured credentials')
        if prompt.endswith(('>', '#')):
            if '(config' in lower or (template != 'c8000v' and prompt.endswith('#')):
                raise DiscoveryError('Console is in configuration mode; exit it before discovery')
            if template == 'c8000v' and prompt.endswith('>'):
                if enabling:
                    # Telnet attachment plus the initial Enter can produce two
                    # unprivileged prompts before enable's password prompt.
                    # Never resend enable or a secret for buffered prompts.
                    if 'enable' not in seen and stale_enable_prompts < 2:
                        stale_enable_prompts += 1
                        continue
                    if 'enable' not in seen:
                        raise DiscoveryError('Enable password prompt not received; inspect the console')
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


def versa_login(console, auth):
    """Read-only login: never perform first-boot setup or change passwords."""
    console.send('')
    seen = set()
    for _ in range(8):
        _, match = console.expect('|'.join((LOGIN, PASSWORD, FAILURE, WIZARD, SHELL, CLI)),
                                  timeout=console.boot_timeout)
        prompt = match.group().strip()
        if re.fullmatch(SHELL, prompt):
            if not prompt.startswith((auth[0] + '@', '[' + auth[0] + '@')):
                raise DiscoveryError('Console belongs to another user; log out before discovery')
            console.prompt = prompt
            return
        if re.fullmatch(CLI, prompt):
            if not prompt.endswith('>'):
                raise DiscoveryError('Console is in configuration mode; exit it before discovery')
            if 'exit' in seen:
                raise DiscoveryError('Exit Versa CLI to the Linux shell before discovery')
            seen.add('exit')
            console.send('exit')
            continue
        if not re.fullmatch(LOGIN, prompt):
            raise DiscoveryError('Device needs initialization or rejected configured credentials')
        stage = 'username' if prompt.lower().endswith(('login:', 'username:')) else 'password'
        if stage in seen:
            raise DiscoveryError('Console authentication failed; not retrying credentials')
        seen.add(stage)
        console.send(auth[0] if stage == 'username' else auth[1])
    raise DiscoveryError('Console login did not complete')


def versa_shell_command(console, command):
    console.send(command)
    output, match = console.expect(SHELL, timeout=console.boot_timeout)
    lines = output[:match.start()].splitlines()
    if lines and lines[0].strip() == command:
        lines.pop(0)
    return '\n'.join(lines).strip()


def versa_identity(console):
    hostname = versa_shell_command(console, 'hostname')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', hostname):
        raise DiscoveryError('Could not read Versa hostname from Linux shell')
    output = versa_shell_command(console, 'ip -o -4 addr show dev eth0 scope global')
    addresses = []
    for value in re.findall(r'\binet\s+(\d+\.\d+\.\d+\.\d+)/\d+', output):
        address = IPv4Address(value)
        if not (address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local):
            addresses.append(str(address))
    if not addresses:
        raise DiscoveryError('No usable IPv4 address on Versa management interface eth0')
    return hostname, list(dict.fromkeys(addresses))


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
                if template not in ('c8000v', 'paloalto', 'panorama') and template not in VERSA_PREFIXES:
                    raise DiscoveryError('Unsupported console discovery template')
                if str(node.get('status')) != '2':
                    raise DiscoveryError('Node is not running')
                url = urlsplit(node.get('url', ''))
                if node.get('console') != 'telnet' or url.scheme != 'telnet' or not url.port:
                    raise DiscoveryError('Working Telnet console required')
                stage = 'loading device credentials'
                auth = credentials(root, prefix=VERSA_PREFIXES.get(template) or
                                   ('CISCO' if template == 'c8000v' else 'PALO'))
                stage = 'opening Telnet console'
                channel = ssh.get_transport().open_session(timeout=10)
                channel.get_pty(term='vt100', width=512, height=1000)
                channel.exec_command('telnet 127.0.0.1 ' + str(url.port))
                console = (PaloConsole if template in ('paloalto', 'panorama') else Console)(channel, boot_timeout=timeout)
                stage = 'logging into console'
                if template in VERSA_PREFIXES:
                    versa_login(console, auth)
                else:
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
                elif template in VERSA_PREFIXES:
                    management_port = 'eth-0/0' if template in ('versafvnf', 'versavnf') else 'eth0'
                    if not any(port['name'] == management_port and str(port.get('network_id')) in management
                               for port in interfaces(client, path, node).values()):
                        raise DiscoveryError('Versa management interface is not connected to pnet1')
                    hostname, addresses = versa_identity(console)
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
