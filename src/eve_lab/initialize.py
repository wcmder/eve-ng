"""Discover running lab consoles and apply per-node initialization files."""
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import paramiko

from .config import load_server
from .deploy import lab_path, named
from .device_console import Console, credentials
from .palo_ssh import management_targets, connect_palo


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

    def initialize(self, commands, username=None, password=None):
        self.command('set cli pager off')
        self.command('configure')
        for command in commands:
            self.command(command)
        output = self.command('commit', timeout=self.boot_timeout)
        if not re.search(r'Configuration committed successfully|There are no changes to commit', output, re.I):
            raise RuntimeError('Palo Alto commit not confirmed; inspect candidate config and commit jobs')
        self.command('exit')


def config_commands(path, template):
    content = path.read_text()
    if any(ord(char) < 32 and char not in '\n\r\t' for char in content):
        raise ValueError('Control characters in init file')
    commands = [line.strip() for line in content.splitlines()
                if line.strip() and not line.lstrip().startswith(('!', '#'))]
    if not commands:
        raise ValueError('Init file is empty')
    if template == 'paloalto':
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
    if management_ip and node_name in nodes and nodes[node_name].get('template') != 'paloalto':
        raise ValueError('--management-ip currently supports Palo Alto nodes only')
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
        address = targets.get(name) if template == 'paloalto' else None
        reason = None
        if template not in ('c8000v', 'paloalto'):
            reason = 'Unsupported init template: ' + str(template)
        elif node.get('console') != 'telnet' and not address:
            reason = 'Console type ' + str(node.get('console')) + ' is unsupported; init requires a working Telnet serial console'
            if template == 'paloalto':
                reason += ' or a Palo management_ip in init.yaml/--management-ip'
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
    logins = {template: credentials(root, prefix='PALO' if template == 'paloalto' else 'CISCO')
              for _, template, _, _ in pending}
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
                if template == 'paloalto' and targets.get(name):
                    device, channel = connect_palo(ssh, targets[name], login[0], login[1], timeout)
                else:
                    channel = ssh.get_transport().open_session(timeout=10)
                    channel.get_pty(term='vt100', width=512, height=1000)
                    channel.exec_command('telnet 127.0.0.1 ' + str(port))
                console = (PaloConsole if template == 'paloalto' else Console)(channel, boot_timeout=timeout)
                console.login(*login)
                print('Applying init to ' + name + '...', file=sys.stderr, flush=True)
                console.initialize(commands, username=login[0], password=login[1])
                result['completed'].append(name)
                if template == 'c8000v':
                    try:
                        result['interface_status'][name] = console.interface_status()
                    except (RuntimeError, ValueError, OSError, paramiko.SSHException):
                        result['warnings'].append({'node': name, 'reason':
                            'Init saved successfully, but interface status could not be read; inspect show ip interface brief'})
                else:
                    result['warnings'].append({'node': name, 'reason': 'Interface IP reporting is currently supported for c8000v only'})
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
