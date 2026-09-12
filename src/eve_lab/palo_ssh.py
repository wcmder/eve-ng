"""Palo management SSH through the EVE host, retaining device host-key checks."""
import ipaddress
from pathlib import Path
import sys
import time

import paramiko
import yaml


def management_targets(root, lab, node_name=None, management_ip=None):
    path = Path(root) / 'labs' / lab / 'init.yaml'
    targets = yaml.safe_load(path.read_text()) if path.exists() else {}
    if targets is None:
        targets = {}
    if not isinstance(targets, dict):
        raise ValueError('init.yaml must map node names to management_ip settings')
    result = {}
    for name, settings in targets.items():
        if not isinstance(name, str) or not isinstance(settings, dict) or set(settings) != {'management_ip'}:
            raise ValueError('Each init.yaml node must contain only management_ip')
        result[name] = str(ipaddress.IPv4Address(settings['management_ip']))
    if management_ip:
        if not node_name:
            raise ValueError('--management-ip requires --node')
        result[node_name] = str(ipaddress.IPv4Address(management_ip))
    return result


def connect_palo(host_ssh, address, username, password, timeout):
    deadline = time.monotonic() + timeout
    while True:
        device = paramiko.SSHClient()
        device.load_system_host_keys()
        tunnel = None
        try:
            remaining = max(1, min(10, deadline - time.monotonic()))
            tunnel = host_ssh.get_transport().open_channel(
                'direct-tcpip', (address, 22), ('127.0.0.1', 0), timeout=remaining)
            device.connect(address, username=username, password=password, sock=tunnel,
                           timeout=remaining, auth_timeout=remaining, banner_timeout=remaining,
                           allow_agent=False, look_for_keys=False)
            return device, device.invoke_shell(term='vt100', width=512, height=1000)
        except paramiko.BadHostKeyException:
            device.close()
            if tunnel is not None: tunnel.close()
            raise RuntimeError('Palo SSH host key changed for ' + address + '; verify the replacement before updating known_hosts') from None
        except paramiko.AuthenticationException:
            device.close()
            if tunnel is not None: tunnel.close()
            raise RuntimeError('Palo SSH authentication failed; check PALO_USERNAME/PALO_PASSWORD and complete first-login password setup via VNC') from None
        except (OSError, paramiko.SSHException) as error:
            device.close()
            if tunnel is not None: tunnel.close()
            if 'not found in known_hosts' in str(error):
                raise RuntimeError('Palo SSH key is not trusted for ' + address +
                                   '; verify and accept its key using SSH from this Mac (ProxyJump through EVE if needed)') from None
            if time.monotonic() >= deadline:
                raise RuntimeError('Timed out waiting for Palo management SSH at ' + address +
                                   ':22; check management IP, SSH service and EVE reachability') from None
            print('Waiting for Palo management SSH at ' + address + ':22...', file=sys.stderr, flush=True)
            time.sleep(min(5, max(0, deadline - time.monotonic())))
