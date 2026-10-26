"""Discover running lab consoles and apply per-node initialization files."""
from pathlib import Path
import re
import sys
from ipaddress import IPv4Address, IPv4Network
from urllib.parse import quote
from urllib.parse import urlsplit

import paramiko

from .config import load_server
from .deploy import lab_path, named
from .device_console import Console, credentials, current_password
from .palo_ssh import management_targets, connect_palo
from .versa_console import (VERSA_PREFIXES, VersaConsole, VersaHeadendConsole,
                            versa_credentials, validate_commands as validate_versa_commands)


class PaloConsole(Console):
    def login(self, username, password, secret=None):
        self.send('')
        for _ in range(8):
            _, match = self.expect(
                r'(?i:login:|username:|password:)\s*$|^[\w.@()/:\-]+[>#]\s*$|(?i:enter new password|new password|old password|confirm password)[^\n]*$',
                timeout=self.boot_timeout)
            prompt = match.group().strip()
            if re.search(r'new password|old password|confirm password', prompt, re.I):
                raise RuntimeError('Complete the mandatory password change in the Palo Alto console first, then update PALO_PASSWORD in .env')
            if prompt.lower().startswith(('login:', 'username:')):
                self.send(username)
            elif prompt.lower().startswith('password:'):
                self.send(password)
            else:
                if prompt.endswith('#'):
                    self.command('exit')
                return
        raise RuntimeError('Palo Alto login failed; check PALO credentials')

    def command(self, command, timeout=60):
        self.send(command)
        output, _ = self.expect(r'^[\w.@()/:\-]+[>#]\s*$', timeout)
        if re.search(r'^\s*(?:Invalid syntax|Unknown command|Server error|Error:|Commit failed)', output, re.M | re.I):
            raise RuntimeError('Palo Alto rejected a command; inspect console (output omitted)')
        return output

    def set_password(self, username, password):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', username or ''):
            raise ValueError('Invalid PALO_USERNAME')
        if not password or any(ord(c) < 32 or ord(c) == 127 for c in password):
            raise ValueError('Invalid PALO_PASSWORD')
        self.send('set mgt-config users ' + username + ' password')
        pattern = (r'(?im:^(?:enter |new |confirm |retype )*password\s*:\s*$|'
                   r'^.*(?:error|invalid|mismatch|failed).*$)|^[\w.@()/:\-]+#\s*$')
        seen = set()
        for _ in range(3):
            _, match = self.expect(pattern, timeout=self.boot_timeout)
            prompt = match.group().strip().lower()
            if prompt.endswith('#'):
                if seen != {'new', 'confirm'}:
                    raise RuntimeError('Palo password change was not confirmed (secrets omitted)')
                return
            if re.search('error|invalid|mismatch|failed', prompt):
                raise RuntimeError('Palo rejected password change (secrets omitted)')
            stage = 'confirm' if 'confirm' in prompt or 'retype' in prompt else 'new'
            if stage in seen:
                raise RuntimeError('Palo repeated password prompt (secrets omitted)')
            seen.add(stage)
            self.send(password)
        raise RuntimeError('Palo password change did not complete (secrets omitted)')

    def initialize(self, commands, username=None, password=None):
        self.command('set cli pager off')
        self.command('configure')
        for command in commands:
            self.command(command)
        if getattr(self, 'change_password', False) and not getattr(self, 'password_changed', False):
            self.set_password(username, password)
        output = self.command('commit', timeout=self.boot_timeout)
        if not re.search(r'Configuration committed successfully|There are no changes to commit', output, re.I):
            raise RuntimeError('Palo Alto commit not confirmed; inspect candidate config and commit jobs')
        self.command('exit')


class PaloSerialConsole(PaloConsole):
    """PAN-OS serial login, including automatic first-login password setup."""

    def login(self, username, password, current_password=None):
        if not password or any(ord(c) < 32 or ord(c) == 127 for c in password):
            raise ValueError('Invalid PALO_PASSWORD')
        self.send('')
        stages = set()
        login_password = current_password or password
        self.password_changed = False
        stale_failures = 0
        pattern = (r'(?im:^[^\n]*login:\s*$|^username:\s*$|^password:\s*$|'
                   r'^(?:enter )?(?:old|new|confirm|retype)[^\n]*password[^\n]*$|'
                   r'^login incorrect\s*$|^authentication failed[^\n]*$)|'
                   r'^[\w.@()/:\-]+[>#]\s*$')
        for _ in range(16):
            _, match = self.expect(pattern, timeout=self.boot_timeout, wake=not stages)
            prompt = match.group().strip().lower()
            if 'incorrect' in prompt or 'authentication failed' in prompt:
                if 'password' not in stages and not {'old', 'new', 'confirm'} & stages and stale_failures < 1:
                    # Shared EVE consoles may still be at a password prompt;
                    # our initial Enter can finish that abandoned attempt.
                    stale_failures += 1
                    stages.clear()
                    print('Discarding an earlier console login failure; waiting for a fresh login prompt...', file=sys.stderr, flush=True)
                    continue
                raise RuntimeError('Palo Alto rejected configured credentials; check PALO_CURRENT_PASSWORD (or PALO_PASSWORD when blank); no fallback attempted')
            if prompt.endswith(('>', '#')):
                if ({'old', 'new', 'confirm'} & stages) and not {'new', 'confirm'} <= stages:
                    raise RuntimeError('Palo Alto did not confirm the mandatory password change; no init commands applied')
                self.password_changed = {'new', 'confirm'} <= stages
                if prompt.endswith('#'):
                    self.command('exit')
                return
            if 'old' in prompt:
                stage, value = 'old', login_password
            elif 'confirm' in prompt or 'retype' in prompt:
                stage, value = 'confirm', password
            elif 'new' in prompt:
                stage, value = 'new', password
            elif prompt.endswith('login:') or prompt.startswith('username:'):
                stage, value = 'username', username
            else:
                stage, value = 'password', login_password
            if stage in stages:
                raise RuntimeError('Palo Alto repeated a login/password-change prompt; inspect password policy and boot readiness (secrets omitted)')
            stages.add(stage)
            if stage == 'password':
                print('Submitting configured Palo Alto credentials...', file=sys.stderr, flush=True)
            elif stage in ('old', 'new', 'confirm'):
                print('Answering Palo Alto ' + stage + '-password prompt (value hidden)...', file=sys.stderr, flush=True)
            self.send(value)
        raise RuntimeError('Palo Alto first-login sequence did not complete')


class PanoramaConsole(PaloSerialConsole):
    """Panorama uses the shared PAN-OS serial login sequence."""


def prepare_panorama_console(client, topology, node_name, check=False):
    if not node_name:
        raise ValueError('--prepare-console requires --node')
    path = lab_path(topology)
    node = named(client, path + '/nodes').get(node_name)
    if not node or node.get('template') not in ('panorama', 'paloalto'):
        raise ValueError('--prepare-console requires a Panorama or Palo Alto firewall node')
    if str(node.get('status')) != '0':
        raise ValueError('Stop ' + node_name + ' before preparing its console')
    result = {'lab': topology['name'], 'node': node_name, 'check': check,
              'previous_console': node.get('console'), 'console': 'telnet', 'changed': False}
    if check or node.get('console') == 'telnet':
        return result
    endpoint = path + '/nodes/' + quote(node['id'], safe='')
    current = client.request('GET', endpoint)
    if any(current.get(k) != node.get(k) for k in ('name', 'template', 'console')) or str(current.get('status')) != '0':
        raise RuntimeError('Node changed or started; console not modified')
    client.request('PUT', endpoint, {'name': node_name, 'console': 'telnet'})
    if client.request('GET', endpoint).get('console') != 'telnet':
        raise RuntimeError('EVE did not persist the Telnet console setting')
    result['changed'] = True
    return result


def validate_panorama_network(commands):
    values = {}
    for line in commands:
        if not line.startswith('set deviceconfig system '):
            continue
        words = line.split()[3:]
        for key in ('ip-address', 'netmask', 'default-gateway'):
            if key in words and words.index(key) + 1 < len(words):
                values[key] = words[words.index(key) + 1]
        match = re.search(r'\bdns-setting servers primary (\S+)', line)
        if match:
            values['dns'] = match.group(1)
    if set(values) != {'ip-address', 'netmask', 'default-gateway', 'dns'}:
        raise ValueError('Panorama factory init needs static management IP, netmask, gateway and DNS in its init file')
    try:
        for value in values.values():
            IPv4Address(value)
        network = IPv4Network(values['ip-address'] + '/' + values['netmask'], strict=False)
        if IPv4Address(values['default-gateway']) not in network:
            raise ValueError('Gateway outside management subnet')
    except ValueError:
        raise ValueError('Invalid Panorama static management network configuration') from None


def config_commands(path, template):
    content = path.read_text()
    if any(ord(char) < 32 and char not in '\n\r\t' for char in content):
        raise ValueError('Control characters in init file')
    commands = [line.strip() for line in content.splitlines()
                if line.strip() and not line.lstrip().startswith(('!', '#'))]
    if not commands and template != 'panorama' and template not in VERSA_PREFIXES:
        raise ValueError('Init file is empty')
    if template in VERSA_PREFIXES:
        validate_versa_commands(commands)
    elif template in ('paloalto', 'panorama'):
        if any(not line.startswith(('set ', 'delete ')) for line in commands):
            raise ValueError('Palo Alto init files must contain only configuration-mode set/delete commands')
    elif any(line.lower().startswith(('banner ', 'macro ', 'reload', 'write ', 'copy ', 'configure ')) for line in commands):
        raise ValueError('Cisco init requires noninteractive configuration-mode commands')
    return commands


def initialize(client, topology, root, server_name, node_name=None, check=False, timeout=600, management_ip=None):
    if not 1 <= timeout <= 3600:
        raise ValueError('--timeout must be between 1 and 3600 seconds')
    nodes = named(client, lab_path(topology) + '/nodes')
    targets = management_targets(root, topology['name'], node_name, management_ip)
    if management_ip and node_name in nodes and nodes[node_name].get('template') not in ('paloalto', 'panorama'):
        raise ValueError('--management-ip supports Palo Alto and Panorama nodes only')
    if node_name is not None:
        if node_name not in nodes:
            raise ValueError('Node not found: ' + node_name)
        nodes = {node_name: nodes[node_name]}
    result = {'lab': topology['name'], 'check': check, 'planned': [], 'completed': [], 'skipped': [], 'failed': [],
              'interface_status': {}, 'warnings': []}
    pending = []
    base = (Path(root) / 'labs' / topology['name'] / 'configs').resolve()
    for name, node in nodes.items():
        template = node.get('template')
        address = targets.get(name) if template in ('paloalto', 'panorama') else None
        reason = None
        if template not in ('c8000v', 'paloalto', 'panorama') and template not in VERSA_PREFIXES:
            reason = 'Unsupported init template: ' + str(template)
        elif node.get('console') != 'telnet' and not address:
            reason = 'Console type ' + str(node.get('console')) + ' is unsupported; init requires a working Telnet serial console'
            if template in ('paloalto', 'panorama'):
                reason += '; stop the node and run eve init <lab> --node <name> --prepare-console, or use --management-ip after initial setup'
        elif not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*', name):
            reason = 'Node name is not a safe config filename'
        path = (base / (name + '-init.cfg')).resolve()
        if not reason and (not path.is_relative_to(base) or not path.is_file()):
            reason = 'Missing init file: configs/' + name + '-init.cfg'
        if reason:
            result['skipped'].append({'node': name, 'reason': reason})
            print(f'Skipped {name}: {reason}', file=sys.stderr)
            continue
        commands = config_commands(path, template)
        if template == 'panorama' and not address:
            validate_panorama_network(commands)
        url = urlsplit(node.get('url', ''))
        if not address and (node.get('console') != 'telnet' or url.scheme != 'telnet' or not url.port):
            raise ValueError('No Telnet console URL advertised for ' + name)
        if str(node.get('status')) == '0':
            raise ValueError('Start ' + name + ' with eve start before initialization')
        result['planned'].append({'node': name, 'template': template, 'file': str(path),
                                  'transport': 'ssh' if address else 'telnet',
                                  'management_ip': address, 'port': 22 if address else url.port})
        pending.append((name, template, 22 if address else url.port, commands))
    if check or not pending:
        return result
    server = load_server(root, server_name, auth='ssh')
    # Validate all credentials before touching devices.
    logins = {template: (versa_credentials(root, template) if template in VERSA_PREFIXES else
                        credentials(root, prefix='PALO' if template in ('paloalto', 'panorama') else 'CISCO'))
              for _, template, _, _ in pending}
    existing_passwords = {template: (login[2] if template in VERSA_PREFIXES else
                          current_password(root, 'CISCO' if template == 'c8000v' else 'PALO', login[1]))
                          for template, login in logins.items()}
    ssh = paramiko.SSHClient()
    try:
        ssh.load_system_host_keys()
        ssh.connect(server.get('ssh_host') or urlsplit(server['url']).hostname,
                    username=server['ssh_username'], password=server['ssh_password'],
                    timeout=10, auth_timeout=10, banner_timeout=10,
                    allow_agent=False, look_for_keys=False)
        for name, template, port, commands in pending:
            channel = None
            device = None
            print(f'Waiting for {name} console (up to {timeout}s per prompt)...', file=sys.stderr, flush=True)
            try:
                login = logins[template]
                existing = existing_passwords[template]
                if template in ('paloalto', 'panorama') and targets.get(name):
                    device, channel = connect_palo(ssh, targets[name], login[0], existing, timeout)
                else:
                    channel = ssh.get_transport().open_session(timeout=10)
                    channel.get_pty(term='vt100', width=512, height=1000)
                    channel.exec_command('telnet 127.0.0.1 ' + str(port))
                console_type = {'paloalto': PaloConsole if targets.get(name) else PaloSerialConsole,
                                'panorama': PanoramaConsole,
                                'versafvnf': VersaConsole, 'versavnf': VersaConsole,
                                'versadir': VersaHeadendConsole,
                                'versaana': VersaHeadendConsole}.get(template, Console)
                console = console_type(channel, boot_timeout=timeout)
                if template == 'versadir':
                    console.sudo_password = True
                if template == 'panorama' or (template == 'paloalto' and not targets.get(name)):
                    if targets.get(name):
                        PaloConsole.login(console, login[0], existing)
                    else:
                        console.login(*login, current_password=existing)
                elif template in VERSA_PREFIXES:
                    console.login(*login)
                else:
                    console.login(login[0], existing, *login[2:])
                if template in ('paloalto', 'panorama'):
                    console.change_password = existing != login[1]
                print('Applying init to ' + name + '...', file=sys.stderr, flush=True)
                console.initialize(commands, username=login[0], password=login[1])
                result['completed'].append(name)
                if template == 'c8000v':
                    try:
                        result['interface_status'][name] = console.interface_status()
                    except (RuntimeError, ValueError, OSError, paramiko.SSHException):
                        result['warnings'].append({'node': name, 'reason':
                            'Init saved successfully, but interface status could not be read; inspect show ip interface brief'})
            except RuntimeError as error:
                result['failed'].append({'node': name, 'reason': str(error)})
                print(f'Failed {name}: {error}; partial changes may remain', file=sys.stderr)
            except (OSError, paramiko.SSHException):
                result['failed'].append({'node': name, 'reason': 'Console transport failed; inspect device before retrying'})
            finally:
                if channel is not None:
                    channel.close()
                if device is not None:
                    device.close()
    except paramiko.BadHostKeyException:
        raise RuntimeError(
            'EVE host SSH key has changed and does not match known_hosts. '
            'Verify the host key through a trusted server console before replacing the saved key; '
            'SSH password authentication has not been attempted') from None
    except paramiko.AuthenticationException:
        raise RuntimeError(
            'EVE host SSH authentication failed; check EVE_SSH_USERNAME and '
            'EVE_SSH_PASSWORD in .env (shell environment overrides .env)') from None
    except paramiko.SSHException as error:
        if 'not found in known_hosts' in str(error):
            raise RuntimeError(
                'EVE host SSH key is not trusted yet. Connect using ssh to the EVE host '
                'and verify its fingerprint before accepting the key') from None
        raise RuntimeError('EVE host SSH handshake failed; check the SSH service and supported algorithms') from None
    except OSError:
        raise RuntimeError('Cannot connect to EVE host SSH; check host reachability and TCP port 22') from None
    finally:
        ssh.close()
    return result
