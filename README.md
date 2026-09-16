# EVE-NG labs

Shared Python tooling for `http://10.0.4.4`, with independent lab definitions.

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

The local, gitignored `.env` contains the supplied credentials. For another checkout,
copy `.env.example` to `.env` and fill in the passwords. Shell environment variables
override `.env`. Server addresses belong in `config/servers.yaml`.

## Commands

Run from the repository root with the virtual environment activated. Examples
using `palo-lab` are illustrative; the current checked-in lab is `palo-lab1`.
Use your actual lab and node names:

| Command | Purpose | Contacts EVE-NG? |
| --- | --- | --- |
| `eve plan <lab>` | Validate the local YAML and summarize object counts. | No |
| `eve apply <lab> [--no-prune]` | Apply the topology; prune undeclared nodes, networks, and stale links by default. | Yes |
| `eve start <lab> [--node <name>]` | Start YAML-declared nodes, or only the named remote node; skip nodes already running. | Yes |
| `eve stop <lab> [--node <name>]` | Stop every remote node, or only the named node; skip nodes already stopped. | Yes |
| `eve delete <lab>` | Stop all remote nodes and permanently delete the entire remote lab. | Yes |
| `eve status [lab]` | Read server statistics, or a lab's nodes and networks when a lab is provided. | Yes |
| `eve backup <lab> [--check]` | Export supported node configs through EVE-NG and save local backups; check reports support only. | Yes |
| `eve bootstrap <lab> --node <name> [--check] [--attach]` | Prepare experimental Palo 11.2 first-boot ISO; optionally attach to a stopped node. Never wipes or starts it. | Yes |
| `eve init <lab> [--node <name>] [--check] [--timeout 600]` | Discover console ports, wait for login, apply per-node init files and save/commit. | Yes |
| `eve restore <lab> --from <backup-directory> [--check] [--wipe]` | Upload and enable saved startup configs; optionally wipe restored nodes for initialization. | Yes |
| `eve templates` | List available device templates. | Yes |
| `eve template <name>` | Fetch template details, image options, and server defaults. | Yes |
| `eve securecrt pnet1 [--username admin]` | Generate a SecureCRT SSH session import script from DHCP leases. | SSH |
| `eve nat status pnet1` | Show current NAT rules and managed pnet1 configuration state. | SSH |
| `eve nat add pnet1 [--dry-run]` | Add runtime Internet NAT through pnet0. | SSH |
| `eve nat remove pnet1 [--dry-run]` | Remove only the NAT rules managed by this command. | SSH |
| `eve dhcp update dns [pnet1] [--dry-run]` | Update DHCP DNS servers from `.env`. | SSH |
| `eve dhcp report pnet1` | List DHCP leases, addresses, hostnames, and expiration times. | SSH |
| `eve dhcp clear pnet1 [--dry-run]` | Back up and clear pnet1 DHCP server leases over SSH. | SSH |

Successful commands print JSON results; progress, skip notices, and errors may
also be printed. API commands log in and log out using an in-memory session
cookie. DHCP, NAT, and SecureCRT generation use SSH; init and bootstrap use both
the API and host SSH when applying changes. Discovery, status, and local plan commands do not modify devices.
The current implementation loads server configuration and credentials even for
`plan`. Starting a VM does not mean the guest OS has finished booting.

### Deploy a lab

```sh
eve template c8000v
eve plan palo-lab
eve apply palo-lab
eve start palo-lab
eve status palo-lab
# When finished:
eve stop palo-lab
```

`apply` validates the YAML, checks template/image availability and network types,
and requires `remote_folder` to exist on EVE-NG. It creates the lab if missing,
adds missing objects, and connects interfaces. Nodes use template defaults with
YAML settings overriding them. Interfaces are resolved from EVE-NG's returned
names and IDs; `GigabitEthernet1`, `Gi1`, and `g1` are equivalent spellings.
`apply` does not start nodes or push guest device configurations from `configs/`.

Reruns match objects by name within the remote lab and skip matching objects and
connections. Explicit `cpu`, `ram`, `console`, `left`, and `top` changes are applied to stopped nodes and
verified afterward; running nodes must be stopped first. Differences in other
settings (such as image) still cause a conflict. Ethernet interface counts can
be increased on stopped nodes. QEMU interface counts can also decrease with
pruning: links to removed ports must be absent from YAML, and existing attachments
on removed ports are disconnected before resizing. This version
does not replace conflicting object settings. With pruning enabled, stopped
interfaces are rewired to match YAML. Deletion
of undeclared nodes/networks and stale links is enabled by default.
Use `--no-prune` to keep undeclared objects and connections.
Canvas positions are preserved when omitted from YAML; explicit positions are applied.
Network type and explicit position changes are also applied, with all nodes stopped. Stop a node before
adding a connection. Default pruning requires every remote node to be stopped. `start`
acts only on declared nodes. `stop` acts on every remote node in the selected lab.
Run one deployment at a time per remote lab.

EVE-NG has no transaction around these operations. A failed apply may leave a
partially created lab; the error lists completed operations. Inspect the lab,
resolve the error, and rerun. New-node interface names can only be verified after
node creation. No automatic rollback or deletion is performed.

### Remove objects deleted from YAML

```sh
eve stop palo-lab
eve apply palo-lab
```

Pruning is enabled by default (`--prune` remains accepted). It makes the expanded
YAML authoritative for nodes, networks, and interface attachments in that
remote lab. After applying the desired topology, it deletes undeclared nodes
and networks. EVE-NG's network-delete handler disconnects attached interfaces;
the client verifies both network removal and the resulting interface state.
Internal bridges generated by direct links are retained. Stale interface attachments are also disconnected when their network remains
declared. For example, moving mgmt from Gi1 to Gi8 adds Gi8 and disconnects Gi1.
The client uses EVE-NG's empty-string interface update and verifies disconnection.
With pruning enabled, existing interfaces are rewired to their declared networks
and stale attachments are removed before checking direct-link exclusivity.
With `--no-prune`, conflicting rewires and shared direct-link bridges remain errors.
Use `eve apply palo-lab --no-prune` for the previous additive behavior.

All remote nodes must be stopped before pruning, including manually added nodes.
Pruning executes without a prompt and includes manually created objects absent
from YAML. Deleted node data is not backed up automatically. Local files and the
remote lab itself remain. With `--no-prune`, apply remains additive. Deletions are
verified; failures report completed operations and can leave partial changes.

### Stop all nodes in a lab

```sh
eve stop palo-lab
# Bypass local YAML entirely, including missing or malformed files:
eve stop palo-lab --remote-folder /
eve stop palo-lab --node pa-a  # Stop only the firewall; routers keep running
eve start palo-lab --node pa-a # Start only the firewall
```

`stop` reads the remote node list and stops every active node, including nodes
removed or renamed in YAML and nodes added manually. It ignores local node,
resource, network, and link validation. Normally only `remote_folder` is read from
YAML; the command's lab argument determines the remote filename. If that folder
was edited, specify the original location with `--remote-folder`.

The explicit folder option works even if the YAML is missing or has syntax errors.
This stops nodes only in the selected lab, not all labs on the server. If one stop
fails, the command still attempts the remaining nodes, checks their remote status,
and reports failures with partial progress. Already stopped nodes are skipped.
After stop requests are accepted, it polls once per second for up to 30 seconds
for all nodes to report stopped (API request timeouts still apply). This handles
EVE-NG acknowledging a request before the VM exits. Errors distinguish accepted
requests from confirmed stops. Deletion uses the same wait before removing a lab.

### Delete a lab

```sh
eve delete palo-lab
eve delete palo-lab --server default
```

`delete` uses the lab name and `remote_folder` in the local topology to select the
remote `.unl` file. It stops every active node in that lab, including nodes added
manually that are absent from the YAML, then deletes the whole remote lab and
verifies it is gone. This removes its nodes, networks, and saved lab configuration.
Local YAML and `configs/` files remain available to recreate it with `eve apply`.

Deletion executes immediately without an interactive prompt. If the lab is already
absent, the command succeeds without changes. If a node cannot be stopped, deletion
aborts; nodes stopped earlier remain stopped. Failures report partial progress.
The local topology file must still exist and pass validation.

### Back up device configurations through EVE-NG

```sh
eve backup palo-lab --check   # Query support; no exports or local files
eve backup palo-lab           # Export supported nodes and download their configs
```

Uses the EVE-NG web/API credentials in `.env`, without an SSH connection. The CLI
does not accept device credentials, but EVE's export script still needs console
access and may require device login credentials; it does not bypass authentication.
The command inspects all current remote nodes in the lab, including nodes
added in the GUI. It uses the Community API's exportable configuration list to
identify support; nodes missing from that list print `Skipped <name> (<template>)`
with a reason. A failure to retrieve that list is an error, not an unsupported result.

EVE-NG's published export support includes Catalyst 8000v; Palo Alto is not listed.
The installed host's capabilities take precedence, so run `--check` once it is
online. Support for the exact installed version/images has not yet been verified.
References: [EVE export API](https://www.eve-ng.net/index.php/how-to-eve-ng-api/),
[EVE Cookbook](https://eve-ng.net/wp-content/uploads/2024/04/EVE-PE-BOOK-6.3-2024.pdf).
This implementation targets the Community API; Pro config-set endpoints differ.

Before exporting, save configuration inside each supported device, e.g. Cisco
`copy running-config startup-config`. EVE's export may require the node to be
running and ready for console access. The command does not start, stop, or wipe
nodes. Export updates EVE's stored startup configuration, then downloads it using
`GET .../configs/<id>` after a successful `PUT .../nodes/<id>/export`.
This is a configuration backup, not a VM disk or snapshot backup.

Successful exports are saved as:

```text
labs/palo-lab/configs/backups/<UTC-timestamp>/
  R0-1.cfg
  R-A-2.cfg
  manifest.json
```

Filenames include the remote node ID to avoid duplicate-name collisions. Existing
backups are preserved. Backup directories are gitignored and created with private
permissions because device configs may contain secrets. Config contents are not
printed. The manifest records saved, skipped, and failed nodes.

Unsupported nodes do not block other backups. Export/download failures are printed
and recorded separately, processing continues, and the command exits nonzero if
any node failed. An empty configuration is a failure. Failed exports never fall
back to downloading an old stored configuration. If nothing was saved, results
are printed but no backup directory is created. Palo Alto requires a separate
device-native backup workflow when the server does not support its export.

### Initialize devices after lab startup

```sh
eve start palo-lab
eve init palo-lab --check        # Preview API console mapping and config files
eve init palo-lab                # Wait for consoles, apply files, save/commit
eve init palo-lab --node R0      # Initialize just one node
eve init palo-lab --timeout 900  # Allow slower boots (seconds per login prompt/commit)
```

`eve init` discovers actual node names, templates and Telnet console ports through
the EVE API, requesting native console URLs with `html5=0` during login, then
uses the host SSH connection to access those consoles. Palo nodes can instead use
management SSH through the EVE host, as described below. VNC nodes without a
management IP are skipped with a reason. With a working Telnet console, you do
not need to specify a port or management IP. Start the lab first; init refuses
stopped target nodes and waits for boot/login prompts on running nodes. Devices
receive an Enter every 10 seconds during Cisco's initial prompt discovery, in case
the first Enter arrived before Telnet or boot was ready. Once authentication begins,
these extra Enter presses stop. Long waits report progress every 30 seconds without
printing console contents. If it remains stuck, cancel with Ctrl+C and inspect
the device console for a setup prompt that needs manual input.
Devices are processed sequentially. `--check` uses only the API and local files, without
logging into devices or applying changes.

Each node uses `labs/<lab>/configs/<exact-node-name>-init.cfg`. Set the desired
hostname and device configuration in that file. For Palo firewalls and Panorama,
SSH/HTTPS service settings also come from the file; init does not append or
override them. The config filename matches the EVE node name, even when the
configured device hostname differs. Edit the file before running init.
Missing files and unsupported templates print a skip reason. Topology, file, and management-network validation runs before device login; device failures are reported individually and cause
a nonzero exit status. Successful earlier changes are not rolled back.

After saving Cisco initialization, the result includes `interface_status`, keyed
by node name, from `show ip interface brief`. Every listed interface includes
`interface`, `ip_address`, `method` (such as `DHCP`), `status`, and `protocol`.
Unassigned interfaces are omitted; if none have an IP, the list is empty. This
reads current primary IPv4 addresses; it does not
renew leases or wait for DHCP. If reporting fails, init remains completed and a
warning is returned. Interface IP reporting is currently Cisco-only and is not
performed by `--check`.

- **c8000v:** configuration-mode IOS XE commands; uses `CISCO_*` credentials,
  provisions the local privilege-15 account and VTY SSH login, then saves with
  `write memory`. SSH key prerequisites are described below.
- **paloalto:** configuration-mode `set`/`delete` commands; uses `PALO_USERNAME`
  and `PALO_PASSWORD` from `.env`. Serial init handles the initial administrator
  password change, applies the `.cfg`, and verifies the commit. Management DHCP,
  hostname, and SSH/HTTPS settings must be supplied in the `.cfg` when desired.
- **panorama:** uses the same PAN-OS serial login and commit flow as firewalls.
  Hostname and SSH/HTTPS settings come from the `.cfg`. The `PANORAMA_*` management
  network settings in `.env` are appended after the file and take precedence
  over its corresponding IP, netmask, gateway, and DNS settings.

For both PAN-OS templates, serial login tries configured credentials first and,
on an explicit authentication failure for `admin`, tries factory credentials
once and handles the mandatory password change. SSH requires the configured
credentials and already-working management access. Avoid unrelated pending
candidate changes, since commit applies the candidate configuration.

Initialization merges commands into the running device and can be invoked again;
it does not wipe or reboot nodes. Interactive commands and multiline constructs
are unsupported. A login prompt does not guarantee every firewall service has
finished booting; if PAN-OS rejects a command or commit, inspect the reported
failure and retry after it is ready. Cisco and Panorama serial init have been
tested live; the Palo firewall serial first-boot test is pending.

### Palo Alto initialization over management SSH

For factory-new VMs, use the serial-console or supported bootstrap ISO workflow
below; management SSH requires initial management access to be configured.

For Palo firewalls and Panorama, `eve init` can open management SSH through the EVE host.
The Mac does not need a direct route to the management subnet. First configure
the administrator password, management address, and SSH access through serial
init, a supported firewall bootstrap ISO, or manual console setup, and commit.
Set `PALO_USERNAME` and `PALO_PASSWORD` in `.env` to the current device credentials. Verify the actual address using `show interface management`.
A DHCP hostname alone does not reliably identify a specific lab node.

From the firewall configuration prompt, management SSH can be enabled with:

```text
set deviceconfig system service disable-ssh no
commit
```

From your Mac, verify and trust the firewall's SSH host key once (replace the IP):

```sh
ssh -J root@10.0.4.4 admin@172.16.1.134
```

This saves the firewall key in the Mac's known_hosts, which `eve init` also checks.
EVE host credentials and firewall credentials are separate. Unknown or changed
firewall keys are rejected; authentication failures do not trigger repeated login
attempts. Connection failures while SSH starts are retried up to `--timeout`.

```sh
eve init palo-lab --node pa-a --management-ip 172.16.1.134 --check
eve init palo-lab --node pa-a --management-ip 172.16.1.134
```

To retain the mapping for `eve init palo-lab`, create `labs/palo-lab/init.yaml`:

```yaml
pa-a:
  management_ip: 172.16.1.134
```

Use your verified management IP; the address above is an example. Each firewall
can have its own entry. `--management-ip` requires `--node` and overrides that
node's saved mapping. `--check` previews the target but does not test SSH access.

The firewall's `configs/pa-a-init.cfg` contains PAN-OS `set`/`delete` commands.
Init logs in, disables paging, enters configuration mode, applies the file, and
commits. It verifies commit success and exits configuration mode. Use a stable
management address and avoid changing the active SSH access in this file, since
losing the connection prevents commit verification. Initial factory password
changes are not handled by the SSH path; use serial init or manual console setup.
The SSH path is covered by local tests; live SSH init has not been confirmed.

References: [Palo management interface status](https://knowledgebase.paloaltonetworks.com/KCSArticleDetail?id=kA10g000000ClgiCAC),
[Palo management services](https://knowledgebase.paloaltonetworks.com/KCSArticleDetail?id=kA10g000000CltrCAC).

### Palo Alto first-boot bootstrap ISO (experimental)

Palo bootstrap runs only at factory-default first boot. A power cycle of an
already-initialized VM is insufficient. A fresh node or an explicitly approved
reset of its writable state is required. The ISO generator currently accepts
PAN-OS 11.2 images only; it does not support the lab's 12.1.7 firewall image. Use
the serial-init workflow for that image.

```sh
eve bootstrap palo-lab --node pa-a --check  # Read node state/options only
eve bootstrap palo-lab --node pa-a          # Build ISO; leave VM unchanged
# After stopping pa-a:
eve bootstrap palo-lab --node pa-a --attach
```

Preparation uses `PALO_USERNAME` / `PALO_PASSWORD` from `.env`, local `openssl`,
and `genisoimage` over EVE host SSH. It creates `config/init-cfg.txt` for management
DHCP and `config/bootstrap.xml` for the administrator and SSH/HTTPS access.
Both files set the hostname to the selected EVE node name, such as `pa-a`.
Each invocation builds a separate ISO for that node; it is not one shared ISO
for all firewalls. Previously generated ISOs must be rebuilt to include the
hostname. An already-initialized guest does not reapply bootstrap just on reboot.
Empty `content`, `software`, and `license` folders complete the package.

No licenses or software updates are included. The admin password is hashed using
the crypt format used by Palo's bootstrap example; it is not stored as plaintext.
The current `*-init.cfg` commands are not translated into bootstrap XML; apply
them later with `eve init` over serial console or management SSH.

Each build creates a private timestamped directory in `.state/bootstrap/<lab>/<node>`
and an isolated directory under `/opt/unetlab/addons/qemu/.eve-bootstrap` on EVE.
The remote layout is `<scope>/<timestamp>/cdrom.iso`, where the scope identifies
the server, lab path, and node ID. QEMU reads this host-side ISO as a virtual CD-ROM;
the ISO is not copied into the guest's filesystem.
This path is visible inside EVE's QEMU runtime jail. Both directories contain an
ISO copy; generated files and hashes are gitignored. The shared base image folder
is not modified, so other Palo VMs do not inherit the bootstrap settings.

`--attach` adds a read-only IDE CD-ROM to that node's QEMU options via the API,
using `index=2` so QEMU does not create a second empty CD-ROM. PAN-OS bootstrap
mounts `/dev/cdrom`, so avoiding multiple drives removes ambiguity about which
media it reads. The command preserves existing options and verifies the saved value. It refuses running
nodes or nodes with unrecognized CD-ROM options. Existing attachments generated
by this command are replaced, including older paths outside the QEMU jail.
It never stops, wipes, starts, or
recreates a VM. The manifest records `previous_qemu_options` for restoring the
original settings in EVE's node editor while the node is stopped. To rebuild an
attached ISO, rerun with `--attach` while stopped; old build directories are
retained. Keep the ISO available until the first-boot test completes.

Sources: [Palo bootstrap requirements](https://docs.paloaltonetworks.com/vm-series/getting-started/bootstrap-the-vm-series-firewall/bootstrap-package),
[KVM ISO attachment](https://docs.paloaltonetworks.com/vm-series/getting-started/bootstrap-the-vm-series-firewall/bootstrap-the-vm-series-firewall-on-kvm),
[Palo's bootstrap XML example](https://github.com/PaloAltoNetworks/panos-bootstrapper/blob/master/bootstrapper/templates/import/bootstrap/bootstrap.xml).

### Panorama and Palo firewall serial initialization

Panorama is not supported by `eve bootstrap`. Read-only inspection of the
`panorama-12.1.5` image found that its bootstrap sanity check requires an external
software-install operation on Panorama-PC; it rejects ordinary firewall
`init-cfg.txt`/`bootstrap.xml` provisioning. A CD-ROM attachment alone cannot
enable that workflow. The command rejects Panorama before generating or attaching
media. Use the Panorama VNC console for the initial administrator password and
management network configuration, then commit, or use the Panorama serial-console
workflow below. See [Panorama initial configuration](https://docs.paloaltonetworks.com/panorama/getting-started/set-up-panorama/set-up-the-panorama-virtual-appliance/perform-initial-configuration-of-the-panorama-virtual-appliance).

Panorama initialization uses `eve init`, not a bootstrap ISO. The 12.1.5 image
contains a serial-login configuration, while EVE's Panorama template defaults to
VNC. Prepare a stopped node's serial console first:

```sh
eve stop palo-lab1 --node pano
eve init palo-lab1 --node pano --prepare-console --check
eve init palo-lab1 --node pano --prepare-console
eve start palo-lab1 --node pano
```

`--prepare-console` changes only the selected node's console type to `telnet` and
verifies EVE saved it. It does not start, wipe, or initialize the device. For new
nodes, `console: telnet` can instead be set under that node in `topology.yaml`.
If YAML explicitly specifies `console: vnc`, update that field to match. All nodes
in the checked-in `palo-lab1` topology now specify `console: telnet`; existing VNC
nodes can be updated by stopping the lab and running `eve apply palo-lab1`, or
by using `--prepare-console` on a stopped node. Panorama serial initialization has been tested on 12.1.5. Firewall serial login
is configured in the inspected 12.1.7 image; its live first-boot test is pending.

Palo Alto firewall nodes also support `--prepare-console`:

```sh
eve stop palo-lab1 --node pa-a
eve init palo-lab1 --node pa-a --prepare-console --check
eve init palo-lab1 --node pa-a --prepare-console
eve start palo-lab1 --node pa-a
eve init palo-lab1 --node pa-a --timeout 1800
```

Firewall serial init uses `PALO_USERNAME` / `PALO_PASSWORD` from `.env` and the
same automatic first-login password handling described below for Panorama.
It applies `labs/<lab>/configs/<node>-init.cfg` and confirms the commit.
Include the desired management settings in that file; for DHCP on the management
interface connected to pnet1 and SSH/HTTPS access, add:

```text
set deviceconfig system type dhcp-client
set deviceconfig system service disable-ssh no
set deviceconfig system service disable-https no
```

This serial workflow does not require a bootstrap ISO. SSH initialization continues
to require working management connectivity and the configured credentials.

Set Panorama management networking in `.env`. This example address is outside
pnet1's current DHCP pool (`172.16.1.100`–`172.16.1.199`):

```dotenv
PANORAMA_MANAGEMENT_IP=172.16.1.99
PANORAMA_NETMASK=255.255.255.0
PANORAMA_GATEWAY=172.16.1.1
PANORAMA_DNS=1.1.1.1
```

All four values are required when any is set. Shell environment values override
`.env`; these settings apply to the Panorama node being initialized. Assign a
different address when initializing another Panorama. They override management
network commands in `configs/<node>-init.cfg`. The prepared
`labs/palo-lab1/configs/pano-init.cfg` sets the hostname and management SSH/HTTPS access.
Edit these configuration-mode `set`/`delete` commands as needed; init does not
automatically override hostname or management service settings. IP values are
validated before any device login; `.env` remains gitignored.

Panorama's documented management interface does not support the VM-Series
DHCP-client configuration. Connecting `pano.e0` to `pnet1` does not enable DHCP.
First-time init requires a static IPv4 address, netmask, gateway, and primary DNS;
no DHCP lease is automatically allocated or reserved.
See [management DHCP limitations](https://docs.paloaltonetworks.com/ngfw/networking/dhcp/configure-the-management-interface-as-a-dhcp-client).

Use `PALO_USERNAME=admin` and the desired nondefault `PALO_PASSWORD` in `.env`:

```sh
eve init palo-lab1 --node pano --check
eve init palo-lab1 --node pano --timeout 1800
```

Palo firewall and Panorama serial initialization automatically handle first-time setup. It tries
the configured credentials first; if authentication explicitly fails for `admin`,
it tries `admin/admin` once and handles the mandatory old/new/confirm password
prompts using `PALO_PASSWORD`. An already-initialized device uses the configured
credentials without a factory-password attempt. It does not reset the node. The command waits
for prompts, applies the init file and management network settings from `.env`,
and commits. Hostname and SSH/HTTPS settings come from the init file. Success requires confirmation of the
commit; a failure may leave a changed password or candidate configuration.
Repeated password-change prompts fail without logging secrets. If initialization
fails after changing the password, the same command can be rerun using `.env` credentials.

For subsequent configuration changes, use `eve init palo-lab1 --node pano`, or
use `--management-ip <address>` / the lab's `init.yaml` to connect over SSH.
SSH uses only the configured credentials and keeps device host-key verification;
automatic factory-password handling is limited to the serial console.
`--check` validates local
files and advertised console settings without logging in to the device.
### Cisco initialization credentials and configuration

`eve init` supports c8000v (IOS XE) and uses your device credentials from `.env`:

```dotenv
CISCO_USERNAME=admin
CISCO_PASSWORD=<device-password>
CISCO_ENABLE_SECRET=<enable-password>
```

On a fresh Cisco boot, init answers `Enter enable secret:` and `Confirm enable
secret:` using `CISCO_ENABLE_SECRET`. It selects `2` at `Enter your selection [2]:`
to save the initial configuration and continue to the CLI. If the device repeats
a secret prompt, init stops with a password-policy/confirmation error instead of
retrying the same value indefinitely. Secret values are never printed. See
[Cisco initial boot security](https://www.cisco.com/c/en/us/td/docs/routers/ir8340/software/configuration/b_ir8340_cg_17-14/m_overview.pdf).

Device credentials are separate from `EVE_SSH_USERNAME` / `EVE_SSH_PASSWORD`.
The connection goes over SSH to the EVE host and then through its local Telnet
console, so the router does not need a management IP. The host needs the `telnet`
command and a verified SSH host key in your known_hosts file. The router must be
running. Close other console sessions while using `eve init`.

Create the init file with IOS XE configuration-mode commands, for example:

```text
hostname R0
interface GigabitEthernet8
 ip address dhcp
 no shutdown
exit
```

Init enters configuration mode, applies each command, and runs `write memory`.
It merges changes into the current configuration; it does not wipe the router.
After the file is applied, init also creates or updates the local user from
`CISCO_USERNAME` / `CISCO_PASSWORD` with privilege 15 and configures VTY lines 0–4:

```text
username <CISCO_USERNAME> privilege 15 password 0 <CISCO_PASSWORD>
line vty 0 4
 login local
 transport input ssh
```

The password is read from `.env` at runtime, not embedded in the script. These
settings are saved by the same `write memory`. Existing console logins must accept
the configured credentials; a fresh unauthenticated console is also supported.
SSH additionally needs a reachable IP and RSA keys. For a fresh router, include
`ip domain name lab.local` and `crypto key generate rsa modulus 2048` after your
hostname in the init file, plus `ip ssh version 2`. Generate keys only on the
first setup: replacing existing keys can prompt for confirmation, which this
script does not handle. See [Cisco SSH configuration](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/sec_usr_ssh/configuration/xe-3s/sec-usr-ssh-xe-3s-book/sec-secure-shell-v2.html).
Multiline banners/macros and interactive commands
are not supported. On failure, earlier commands may already have changed the device.

### Restore or initialize from a backup

Use a directory created by `eve backup`, containing `manifest.json` and its config
files. Replace `<UTC-timestamp>` with the actual backup directory name:

```sh
eve restore palo-lab --from labs/palo-lab/configs/backups/<UTC-timestamp> --check
eve stop palo-lab
eve restore palo-lab --from labs/palo-lab/configs/backups/<UTC-timestamp>
```

The default uploads and enables EVE's stored startup configs. It does not change
the guest's current configuration or erase its existing writable state. To boot
the nodes from the backup instead:

```sh
eve restore palo-lab --from labs/palo-lab/configs/backups/<UTC-timestamp> --wipe
eve start palo-lab
```

**`--wipe` erases the writable VM state of the restored nodes.** A configuration
backup does not preserve other disk contents. Restore requires those nodes to be
stopped and does not stop or start them automatically. Every upload is read back
and verified before any wipe. `--check`, including with `--wipe`, previews the
mapping and actions without changing the server; running nodes are shown in the
preview but must be stopped before executing restore.

For a new lab, create its nodes with `eve apply <lab>` first, then restore with
`--wipe` and start it. Files map by exact node name, so remote node IDs may differ
from the backup. Templates, images, and Ethernet counts must match; older backups
without image/interface metadata produce compatibility warnings. Missing nodes or
mismatches fail before uploads. Nodes without advertised startup-config support
are reported and skipped, including Palo Alto when unsupported by the host.

This uses the Community web/API credentials in `.env`. Paths are absolute or
relative to the current working directory. Restore overwrites stored startup
configs for matched nodes; partial failures report completed actions without
rollback. Live restore behavior on this host has not been verified.

### pnet1 Internet NAT through pnet0

```sh
eve nat status pnet1
eve nat add pnet1 --dry-run
eve nat add pnet1
eve nat remove pnet1 --dry-run
eve nat remove pnet1
```

`status` is read-only. It reports all IPv4 NAT table rules (including WireGuard),
the managed pnet1 rules and jumps, and whether IPv4 forwarding is enabled.
`managed_state` is `absent`, `configured`, `legacy`, or `unexpected`. Configured
means the managed rule and link are installed, not that guest Internet connectivity
has been tested. Legacy means the previous exclusion rules are still installed;
run add to migrate them. Unexpected rules are shown for inspection.

Uses `.env` SSH credentials and requires root. `--server <name>` selects the host.
Add detects pnet1's IPv4 subnet and masquerades traffic from that subnet leaving
pnet0, excluding the WireGuard subnet `172.16.0.0/24`. Other destinations, including
private networks reached through pnet0, are NATed. This is IPv4 only. Guests must use
the host's pnet1 address as their gateway (currently `172.16.1.1`) and a working
DNS server. No guest configuration is changed.

The host must already have IPv4 forwarding enabled, Internet routing via pnet0,
and an unrestricted FORWARD chain with ACCEPT policy (as on the inspected host).
Custom forwarding rules cause add to stop for manual review. The command does not
change routing, forwarding policy, or the host-wide forwarding sysctl.

The effective match is `-s 172.16.1.0/24 ! -d 172.16.0.0/24 -o pnet0 -j MASQUERADE`
(with the source subnet detected from pnet1). Add also migrates the previous
managed private/reserved exclusion rules to this WireGuard-only exemption.

Rules use the dedicated `EVE_PNET1_NAT` chain and a tagged POSTROUTING link. Repeated
add/remove calls are safe; unrelated rules, including WireGuard NAT, remain intact.
Unexpected rules in the managed chain or unrecognized references cause an error.
A dry run performs read-only checks and lists the proposed commands. Runtime
changes are **not persisted across host reboots**; rerun add after reboot.
Remove stops NAT for new connections; existing tracked NAT connections may retain
their mappings until they expire. No connection tracking entries are flushed.
Failures report completed commands without automatic rollback.

### Update pnet1 DHCP DNS

Set the comma-separated IPv4 DNS servers in `.env`:

```dotenv
EVE_DHCP_DNS=8.8.8.8,1.1.1.1
```

```sh
eve dhcp update dns --dry-run
eve dhcp update dns
eve dhcp update dns pnet1 --server default
```

`pnet1` is the default and currently the only supported interface. Shell environment
values override `.env`. The command uses `EVE_SSH_USERNAME`/`EVE_SSH_PASSWORD`,
validates the dedicated DHCP service and proposed dnsmasq configuration, backs up
`/etc/eve-dhcp/pnet1.conf` beside the original with a `.backup-<timestamp>` suffix,
and restarts `eve-pnet1-dhcp.service`. The JSON result includes the backup path.
If restart fails, it attempts to restore the previous configuration and service.
No restart or backup occurs when the configuration already matches. `--dry-run`
reports the proposed DNS settings without writing files or restarting DHCP.

The dedicated EVE host DHCP configuration, `/etc/eve-dhcp/pnet1.conf`, advertises
Google and Cloudflare DNS to pnet1 clients:

```ini
dhcp-option=option:dns-server,8.8.8.8,1.1.1.1
```

Include this option when rebuilding the host's DHCP service. Existing clients
receive the new DNS servers on DHCP renewal; changing this option does not clear
their leases. Panorama's static DNS is configured separately through
`PANORAMA_DNS` in `.env`.

### Report pnet1 DHCP leases

```sh
eve dhcp report pnet1
eve dhcp report pnet1 --server default
```

Returns JSON with the lease count and each lease's IP address, MAC address,
hostname, client ID, UTC expiration time, remaining seconds, and status
(`active`, `expired`, or `permanent`). Unknown hostnames/client IDs are `null`;
permanent leases have `null` expiration and remaining seconds. These are server
records, not a check that clients are online. The report reads the dedicated
pnet1 lease file without stopping DHCP, creating backups, or changing leases.
It uses the same `.env` SSH credentials as `clear` below.

### Generate SecureCRT SSH sessions

```sh
eve securecrt pnet1
eve securecrt pnet1 --username admin
eve securecrt pnet1 --interactive --username admin
eve securecrt pnet1 --interactive --credentials eve-default
eve securecrt pnet1 --username admin --port 22 --output .state/securecrt-eve.py
```

Fetches the DHCP report over SSH and writes `.state/securecrt-eve.py` by default.
Use `--server <name>` for another configured server. The default output directory
is gitignored. Explicit `--output` paths are relative to your working directory;
rerunning the generator replaces that output file with the latest report.

In SecureCRT, select **Script → Run** and choose the generated Python file.
It creates SSH2 sessions inside the **eve** Session Manager folder, for example
`eve/Router - 172.16.1.109`. Close and reopen Session Manager if needed to refresh.
This uses SecureCRT's [session scripting API](https://www.vandyke.com/support/tips/importsessions.html).
The generated script runs inside SecureCRT, not with your shell's Python.

With `--interactive`, the terminal prompts for each IP's session name. Press
Enter to accept its hostname (or IP if missing), or enter a name such as `R1`.
If a default name is already used in this export, its IP is appended. Duplicate
names (ignoring case) and invalid names prompt again. Ctrl+C or end-of-input
cancels without replacing the output file. Existing SecureCRT sessions are checked
only when you run the generated script. Matching names are updated with the generated IP, port, and login selection.
Without `--interactive`, names continue to include both hostname and IP.

Only active and permanent lease records are included, with one session per IP.
Missing hostnames fall back to the IP address. DHCP does not identify the EVE lab,
so sessions share the `eve` folder. Existing session names are matched for updates; no sessions
are deleted. If DHCP changes an address or hostname, a new session may be created;
remove obsolete entries in SecureCRT as needed.

For a **named saved credential** under SecureCRT's **Global Options → General →
Credentials**, use `--credentials eve-default`. This links new sessions to that
credential title, so future username/password changes in the credential manager
apply to those sessions. The title must already exist in the SecureCRT configuration
where you run the script; the generator does not validate or create credential sets.
No password is read or exported. `--credentials` cannot be combined with `--username`.
Existing sessions with matching names are updated rather than skipped. The script
sets SSH2, the reported IP, the selected port, and the credential title. Without
`--credentials`, it clears the credential reference and sets the supplied username
(or blank if omitted). Other session settings remain unchanged. The script reports
created and updated counts. Matching is by full session name under `eve`, not IP
address. No other sessions are removed.
SecureCRT documents named credentials
[here](https://www.vandyke.com/support/tips/how-to-manage-credentials-in-securecrt-securefx.html).

New sessions use SecureCRT's Default session settings, with protocol, address,
and port replaced (port defaults to 22). Use `--credentials` to select saved
credentials, or `--username` to set a device username (otherwise blank).
The generated script contains only the credential title, not its saved password.
The generator does not export `.env` passwords.
The `.env` SSH credentials are used only to retrieve leases from EVE-NG.
Devices must have SSH enabled and their management addresses must be reachable
from your workstation; the script does not configure SSH or connect to devices.

### Clear pnet1 DHCP leases

```sh
eve dhcp clear pnet1 --dry-run
eve dhcp clear pnet1
```

The dry run checks the host configuration and reports the current lease count.
Clearing stops `eve-pnet1-dhcp.service`, backs up
`/var/lib/eve-dhcp/pnet1.leases` beside the original with a timestamp suffix,
empties the lease file, and restarts the service. Output includes the backup path
and the number of records before clearing and after restart. Other interfaces'
lease files are not touched. The service is briefly unavailable during clearing.

This resets server records; it does not send DHCP release requests from clients
or remove their current IP addresses. Clients can immediately renew leases.
Stop DHCP clients before clearing and restart them afterward to reacquire leases;
for clients in this lab, use `eve stop palo-lab` and `eve start palo-lab`.
Clients in other labs on the same `pnet1` also share this DHCP pool.

The command uses Paramiko locally and Python 3 on EVE-NG. It connects to the
configured server URL's host; optional `ssh_host` overrides that host. Configure
both logins separately in the local, gitignored `.env`:

```dotenv
EVE_USERNAME=admin
EVE_PASSWORD=eve
EVE_SSH_USERNAME=root
EVE_SSH_PASSWORD=eve
```

These are the current server's credentials. After changing its SSH password,
update `EVE_SSH_PASSWORD` in `.env`; the next DHCP command uses the new value.
Shell environment variables override `.env`. Enter values without surrounding
quotes. `config/servers.yaml` maps `ssh_username_env` and `ssh_password_env` to
these variable names. DHCP commands require only the SSH credentials. Lab
commands use web/API credentials; applying init or building bootstrap media also
requires EVE host SSH credentials.

SSH password authentication is automatic, without an interactive prompt or SSH
agent/key authentication. Passwords are not passed as command-line arguments.
The host key must already be trusted in `~/.ssh/known_hosts`; for a new server,
connect with `ssh root@10.0.4.4` and verify its fingerprint before accepting it.
The DHCP helper requires root access.

Use `--server <name>`
to select a server. Only the inspected, dedicated pnet1 dnsmasq configuration is
supported; unexpected service/configuration layouts are rejected.

### Replace the saved SSH key after rebuilding the EVE VM

When you rebuild or replace the EVE VM at `10.0.4.4`, its SSH host key may change.
After confirming the replacement was intentional, run these commands on your Mac:

```sh
ssh-keygen -R 10.0.4.4
ssh -o StrictHostKeyChecking=accept-new root@10.0.4.4
```

The first command removes the old saved key. The second automatically trusts the
new key and prompts for the EVE host's SSH password. Exit the SSH session, then
retry your command, for example:

```sh
eve init palo-lab --node c8kv-0
```

Repeat these steps after future intentional VM replacements. `accept-new` still
rejects changed keys when an existing entry is present; it does not disable host
key checking. Update `.env` if the rebuilt VM also has a different SSH password.

### Deployment errors

For `Failed to create network (11)` during start, the CLI waits 1 second and
rechecks the node before retrying, then waits 2 seconds before a final attempt.
After the third network error, it waits 3 seconds and checks the node status
once more before reporting failure. If any of these checks reports running,
start succeeds without another request. Other API failures are not retried;
persistent network-creation failures are reported after three attempts.
This handles transient failures but does not repair the host's network setup.

HTTP failures include the server's JSON error message when available. A timeout
identifies the request and does not imply that the server cancelled it. The default
server timeout is 120 seconds, configured in `config/servers.yaml`.

If EVE-NG reports `Failed to lock the lab (60061)`, stop retrying writes and inspect
the server's PHP/web error logs and lab lock. A failed server request can leave a
`.unl.lock` file behind. Confirm no active operation owns it before recovering the
lock, then rerun `eve apply`; existing matching lab objects will be reused.

The server's API error log is `/opt/unetlab/data/Logs/api.txt`. For example,
`Undefined array key "left"` indicates missing node coordinates. The client sends
default `left: 200` and `top: 200` when the YAML omits them; explicit positions
override these defaults. Node creation also sends `numberNodes: 1` and a blank
UUID, matching the web UI's single-node request.

For CPU/RAM updates, the client also sends the node's unchanged name. This works
around EVE-NG versions that otherwise return `Cannot edit node (20026)` because
resource-only edits do not mark the node as modified for saving.
New networks are created with `visibility: 1` because EVE-NG discards hidden
bridges that have no connections yet. This lets later requests attach interfaces.

### Options and help

```sh
eve --help
eve apply --help
eve template --help
eve apply palo-lab --server default
eve status palo-lab --server default
eve --root /path/to/eve-ng plan palo-lab
```

`--server <name>` follows the subcommand and selects an entry in
`config/servers.yaml`; it defaults to `default` (`http://10.0.4.4`). `--root <path>`
goes before the subcommand and defaults to the current working directory. Activate
the project's virtual environment before using `eve`, or invoke `.venv/bin/eve`
from the repository root.

## Topology format

Each lab lives in `labs/<name>/` with a `topology.yaml` and `configs/`.
Keep usage instructions and lab-specific notes in this root README.
The topology's `name` must match its directory. This is the project's declarative
format, not a native EVE-NG import file. Deployment currently supports QEMU nodes
and Ethernet links from nodes to networks or directly between nodes.

```yaml
name: palo-lab
description: C8000V router connected to a Palo Alto firewall
remote_folder: /
nodes:
  - name: R1
    template: c8000v
    type: qemu
    image: c8000v-17.15.06
    cpu: 4
    ethernet: 4
    ram: 8192
  - name: PA1
    template: paloalto
    type: qemu
    image: paloalto-11.2.10-h6
    cpu: 2
    ram: 8192
    ethernet: 4 # Management plus three data interfaces.
networks:
  - name: mgmt
    type: pnet1
links:
  - node: R1
    interface: GigabitEthernet1
    network: mgmt
  - node: PA1
    interface: mgmt
    network: mgmt
  - name: r1-pa1
    from:
      node: R1
      interface: GigabitEthernet2
    to:
      node: PA1
      interface: eth1/1
```

Use the image directory's basename: `/opt/unetlab/addons/qemu/c8000v-17.15.06`
becomes `c8000v-17.15.06`. `cpu` is the vCPU count, `ethernet` is the Ethernet
interface count, and optional `ram` is RAM in MB. Optional node fields also include
`console`, `left`, and `top`; networks accept optional `left` and `top` positions.
After editing `cpu` or `ram`, run `eve stop <lab>` and `eve apply <lab>` to update
existing nodes, then `eve start <lab>` when ready. Ethernet interface counts can also be increased on stopped nodes. QEMU counts can decrease with pruning enabled after removing YAML links to the
ports being removed. Other node types require manual downsizing.

Add entries under `links` to connect additional interfaces; increasing `ethernet`
alone does not connect them. Place guest configuration templates in each lab's
`configs/` directory and keep secrets out of templates.

`plan` checks required fields, unique names, positive resource counts, link
references, and duplicate interface assignments. It does not contact the server,
validate hardware capacity or guest compatibility, or compare the remote lab.

### Direct node-to-node links

```yaml
links:
  - name: r1-pa1
    from:
      node: R1
      interface: GigabitEthernet2
    to:
      node: PA1
      interface: eth1/1
```

Both nodes must be declared in `nodes`. The optional `name` identifies the internal
bridge; omit it to generate a stable name from the endpoints. Do not also declare
that bridge under `networks`. Each interface can appear in only one link.

`apply` creates the bridge visibly, connects both endpoints, then hides it so
EVE-NG displays a direct cable. To convert an existing connection, use its bridge
name; matching attachments are reused. A bridge with other attached interfaces
is rejected rather than hidden. `plan` counts the direct link as one YAML link;
its generated bridge is not included in the declared network count.

## Structure

- `README.md`: shared command usage and notes for all labs.
- `src/eve_lab/`: shared configuration, API client, topology validation, deployment, and CLI.
- `config/servers.yaml`: named server connections.
- `labs/palo-lab1/`: current lab definition and device configurations.
- `.state/`: generated bootstrap artifacts and SecureCRT import scripts; deployment reads remote objects directly.
- `tests/`: deployment, validation, and API error tests using a simulated server.

## Verification

```sh
python -m unittest discover -s tests -v
```

API references: [EVE-NG API](https://www.eve-ng.net/index.php/how-to-eve-ng-api/)
and [evengsdk interface connection implementation](https://ttafsir.github.io/evengsdk/api_reference/).
