# EVE-NG labs

Shared Python tooling for EVE-NG, with independent lab definitions.

## Setup

Requires Python 3.11 or newer. Run these commands from the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Create your local credentials file on a new checkout (keep an existing `.env`):

```sh
cp .env.example .env
```

Fill in your EVE-NG web/API and host SSH usernames and passwords. Set `CISCO_*`
and `PALO_*` credentials when using device initialization, backup, or restore.
The `.env` file is gitignored; do not share it. Shell environment variables override
`.env`. Enter values without surrounding quotes.

### Configure your EVE-NG address

Edit `config/servers.yaml` and replace the checked-in `url` with your own EVE-NG
web address, including `http://` or `https://` and a port if needed. For example,
replace `eve.example.com` below with your server's IP address or hostname:

```yaml
servers:
  default:
    url: http://eve.example.com
    username_env: EVE_USERNAME
    password_env: EVE_PASSWORD
    timeout: 120
    ssh_username_env: EVE_SSH_USERNAME
    ssh_password_env: EVE_SSH_PASSWORD
```

SSH uses the URL's host by default. If SSH uses a different IP or hostname, add
`ssh_host: your-ssh-host` under `default`. The address is read from this YAML file,
not an `EVE_HOST` or `EVE_URL` variable in `.env`. You can add other named entries
under `servers` and select one with `--server <name>` after the subcommand.

In SSH examples below, replace `<eve-host>` with your SSH IP or hostname and
replace `root` if your `EVE_SSH_USERNAME` differs. Verify and trust the host key
before running commands that use SSH:

```sh
ssh root@'<eve-host>'
```

Check your API connection without changing the server:

```sh
eve status --server default
```

### Adapt the lab network

The EVE-NG host address and guest device management addresses are separate.
The `172.16.1.x` addresses, gateway, and DHCP pool shown below describe the example
lab; substitute your own management subnet and device addresses. Review the
selected lab's `configs/*-init.cfg` files before initialization, especially static
IPs, netmasks, gateways, and DNS servers. Also update any management addresses in
`labs/<lab>/init.yaml`, `.env` `*_MANAGEMENT_IP` entries, or command-line options.
Changing the server URL does not change these guest settings.

Check `labs/<lab>/topology.yaml` for images installed on your server and the correct
cloud network (the example uses `pnet1` for management). DHCP helpers require the
dedicated dnsmasq service/configuration described below; they do not provision it
on a new EVE-NG host.

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
| `eve backup <lab> [--check]` | Read Cisco/Palo/Panorama configs using device credentials; check previews targets. | Yes |
| `eve bootstrap <lab> --node <name> [--check] [--attach]` | Prepare experimental Palo 11.2 first-boot ISO; optionally attach to a stopped node. Never wipes or starts it. | Yes |
| `eve init <lab> [--node <name>] [--check] [--timeout 600]` | Discover console ports, wait for login, apply per-node init files and save/commit. | Yes |
| `eve restore <lab> --from <backup-directory> [--node <name>] [--check]` | Import saved configs through device consoles, replace/load, and save/commit. | API + SSH |
| `eve templates` | List available device templates. | Yes |
| `eve template <name>` | Fetch template details, image options, and server defaults. | Yes |
| `eve securecrt <lab> [--credentials <title>]` | Read hostnames and IPs through device consoles and generate SSH sessions. | API + SSH |
| `eve securecrt pnet1 [--username admin]` | Generate SecureCRT SSH sessions from DHCP leases and `.env` management IPs. | SSH |
| `eve nat status pnet1` | Show current NAT rules and managed pnet1 configuration state. | SSH |
| `eve nat add pnet1 [--dry-run]` | Add runtime Internet NAT through pnet0. | SSH |
| `eve nat remove pnet1 [--dry-run]` | Remove only the NAT rules managed by this command. | SSH |
| `eve dhcp update dns [pnet1] [--dry-run]` | Update DHCP DNS servers from `.env`. | SSH |
| `eve dhcp report pnet1` | List DHCP leases, addresses, hostnames, and expiration times. | SSH |
| `eve dhcp clear pnet1 [--dry-run]` | Back up and clear pnet1 DHCP server leases over SSH. | SSH |

Successful commands print JSON results; progress, skip notices, and errors may
also be printed. API commands log in and log out using an in-memory session
cookie. DHCP and NAT use SSH. Console backup, console-based SecureCRT discovery,
init, and bootstrap use both the API and host SSH; legacy SecureCRT lease
discovery uses SSH. Discovery, status, and local plan commands do not modify devices.
The current implementation loads server configuration and credentials even for
`plan`. After start requests, the CLI polls selected nodes once per second (up to 30
waits) and requires three consecutive running observations before reporting
success. A stopped/exited VM produces an error listing current running and
non-running nodes. This verifies VM process state, not guest boot completion.

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
Explicit network positions can be updated while nodes run. Network type changes
are deferred only when the network has running attachments. Stop a node before
adding a connection. Default apply preserves running node settings and ports, reporting them in
`deferred`. Stopped node attachments remain editable on shared networks. `start`
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
YAML authoritative for stopped nodes and unprotected networks and attachments
in that remote lab. After applying the desired topology, it deletes undeclared nodes
and networks. EVE-NG's network-delete handler disconnects attached interfaces;
the client verifies both network removal and the resulting interface state.
Internal bridges generated by direct links are retained. Stale interface attachments are also disconnected when their network remains
declared. For example, moving mgmt from Gi1 to Gi8 adds Gi8 and disconnects Gi1.
The client uses EVE-NG's empty-string interface update and verifies disconnection.
With pruning enabled, existing interfaces are rewired to their declared networks
and stale attachments are removed before checking direct-link exclusivity.
With `--no-prune`, conflicting rewires and shared direct-link bridges remain errors.
Use `eve apply palo-lab --no-prune` for the previous additive behavior.

Pruning skips running nodes, including manually added nodes, and leaves their
interface attachments unchanged. Stopped and new nodes can connect, disconnect,
rewire, and resize on both cloud and internal bridge networks while peers run.
Networks have no blanket protection: positions can change, missing networks can
be created, and undeclared networks without running attachments can be removed.
Deleting or changing the type of a network with running attachments is deferred
to avoid disrupting those nodes. A direct link involving a running endpoint may
be only partly connected; its final visibility/exclusivity check is deferred.
The JSON `deferred` list reports these exceptions. Stop the affected running
nodes and rerun apply to finish them.
Pruning executes without a prompt and includes stopped manually created objects
absent from YAML. Deleted node data is not backed up automatically. Local files and the
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

### Back up device configurations

```sh
eve backup palo-lab1 --check             # Preview targets; no device login/files
eve backup palo-lab1                     # Read running configurations
eve backup palo-lab1 --node pano         # Back up one device
eve backup palo-lab1 --timeout 900        # Allow longer console reads
```

The default backup method uses our own device login, rather than EVE's export
scripts. It discovers nodes and native Telnet console URLs through the EVE API,
connects through EVE host SSH, and authenticates using `CISCO_*` or `PALO_*` from
`.env`. Devices must be running, initialized, and ready for console access.
Close other console sessions first. Backup never answers first-boot setup or
password-change prompts and never applies configs, saves, commits, or wipes nodes.

- **c8000v:** reads `more system:running-config` and requires the final `end`.
  Saves IOS configuration as `.cfg`, including running changes not yet saved to NVRAM.
- **paloalto / panorama:** reads `show config running` with XML operational output
  enabled, checks for complete XML, then resets XML operational output to off.
  Saves `.xml`. This captures committed running configuration, not pending
  candidate edits, VM disks, licenses, logs, or a complete device-state export.

Both paths disable CLI paging. PAN-OS backup can also use management SSH through
EVE, using the lab's `init.yaml` mapping or
`eve backup palo-lab1 --node pano --management-ip 172.16.1.99`.
SSH host-key checking remains enabled. `.env` API, host SSH, and device credentials
are separate. `--check` previews transport eligibility without testing credentials
or boot readiness. Unsupported templates and stopped/unusable console targets are
skipped; per-device failures do not prevent remaining backups and cause a nonzero
exit status. No old EVE export is used as a fallback.

Files are saved locally in the repository:

```text
labs/palo-lab1/configs/backups/<UTC-timestamp>/
  rt-0-1.cfg
  pa-a-3.xml
  pano-8.xml
  manifest.json
```

Filenames use the EVE node name and ID. Each run creates a new directory; existing
backups remain. Directories use private permissions (`0700`), files use `0600`,
and backups are gitignored. Config contents are not printed. The manifest records
saved, skipped, and failed nodes, transport, and configuration format. If no
configuration is saved, no backup directory is created.

`eve restore` accepts Cisco `.cfg` and Palo/Panorama `.xml` from these backups.
It loads them directly through device consoles, as described below. XML capture
validation checks completeness, not full device-state recovery or cross-version
compatibility. Device-registration authentication keys and trust state are not
restored by these config backups.

The old EVE export method remains available explicitly:

```sh
eve backup palo-lab1 --method api --check
eve backup palo-lab1 --method api
```

This method uses EVE's advertised export support and server-side scripts, which
do not receive the local `CISCO_*`/`PALO_*` credentials. It can fail if those scripts
cannot log into the device. `--node` and `--management-ip` are console-method options.
API exports use the same timestamped directory layout with `.cfg` files.

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
  Hostname, SSH/HTTPS, management IP, netmask, gateway, and DNS settings come
  from the per-node `.cfg`. Credentials come from `.env`.

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

From your workstation, verify and trust the firewall's SSH host key once
(replace `<eve-host>` and the firewall IP with your own addresses):

```sh
ssh -J root@'<eve-host>' admin@172.16.1.134
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

Set Panorama management networking in `labs/<lab>/configs/<node>-init.cfg`.
The prepared `labs/palo-lab1/configs/pano-init.cfg` includes:

```text
set deviceconfig system ip-address 172.16.1.99 netmask 255.255.255.0 default-gateway 172.16.1.1
set deviceconfig system dns-setting servers primary 8.8.8.8
set deviceconfig system dns-setting servers secondary 1.1.1.1
```

In the example lab, this address is outside the DHCP pool (`172.16.1.100`–`172.16.1.199`).
Choose an unused address in your own management subnet, outside your DHCP pool.
Use a separate `<node>-init.cfg` with a distinct IP for each Panorama.
Init applies the file and commits; it does not read `PANORAMA_*` network settings
from `.env` or override the file's hostname, management services, or networking.
Administrator credentials still come from `PALO_USERNAME`/`PALO_PASSWORD` in `.env`.
The prepared file also enables management SSH/HTTPS and sets the hostname.

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
for prompts and applies the init file,
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

### Restore device configurations from a backup

Use a timestamped directory created by `eve backup`, containing `manifest.json`
and its config files:

```sh
eve restore palo-lab1 --from labs/palo-lab1/configs/backups/<timestamp> --check
eve restore palo-lab1 --from labs/palo-lab1/configs/backups/<timestamp> --node pano
eve restore palo-lab1 --from labs/palo-lab1/configs/backups/<timestamp>
```

Restore uses the EVE API only to discover and recheck nodes and console URLs.
It does not call EVE startup-config import/export or wipe endpoints. Target
devices must be **running and initialized**, with working console credentials
and IP connectivity to the EVE host for SCP. For fresh nodes, run `eve init`
first to establish credentials and management networking. Close other console
sessions before restore. The old `--wipe` option has been removed.

- **Cisco c8000v:** imports the complete `.cfg` to bootflash using SCP, verifies
  its MD5 checksum against the local file, runs `configure replace ... force`,
  and confirms `write memory`. This replaces configuration instead of merging
  individual commands, so multiline banners and certificate sections remain files.
- **Palo firewall / Panorama:** imports the `.xml` using SCP, runs `load config from`
  in configuration mode, and commits. Load and commit must be confirmed.

Restore replaces device configuration, including saved hostname, networking,
accounts and service settings. Unlike init, it does not append `.env` network
settings or recreate your administrator account. Existing candidate edits on
PAN-OS are replaced. Use the credentials currently on the device in `.env` to
log in; afterward, credentials from the restored config may take effect. Encrypted
secrets may require the original device master key. Configuration backups do not
recreate licenses or Panorama registration/trust state; re-registration may be
needed on rebuilt firewalls. Device-native load can reject an incompatible backup.

Files map by exact EVE node name, not old node ID. Template, image and Ethernet
counts must match when metadata is present. Missing metadata produces warnings.
Complete Cisco configs and PAN-OS XML are validated before device access.
`--check` reads local files and API metadata only: it does not test device login,
SCP routing, host trust or whether the device will accept the config. It can
preview stopped nodes, but applying restore requires them to be running.

Console login uses `CISCO_*` / `PALO_*` credentials, through EVE host SSH as with
init. Palo/Panorama can instead use management SSH through EVE via `init.yaml`
or `--node <name> --management-ip <address>`. Prefer serial access when restoring
management settings, since SSH may disconnect before commit confirmation.

For transfer, the command stages files in a private `0700` directory under
`/tmp/eve-restore-<random>` on EVE, with files set to `0600`, then the device pulls
them using the EVE host's `EVE_SSH_USERNAME` / `EVE_SSH_PASSWORD`. Passwords are
sent only in response to a password prompt, not embedded in SCP commands. New
SCP host keys are accepted only when their fingerprint matches EVE public keys
read through the already verified host SSH connection; changed or unverifiable
keys fail. The default transfer address is EVE's pnet1 IPv4 address. Override it
with `--transfer-host <IPv4>` if devices must reach EVE through another address.

```sh
eve restore palo-lab1 --from labs/palo-lab1/configs/backups/<timestamp> \
  --node pano --transfer-host 172.16.1.1 --timeout 900
```

`--timeout` defaults to 600 seconds per console transfer/load/commit wait.
Staged host files are removed afterward; cleanup failures are reported. Imported
files remain on the devices. Per-device failures are reported and processing
continues for other nodes; any failure causes a nonzero exit status. Partial
changes may remain, and no automatic rollback is performed. Restore has automated
test coverage; live configuration replacement has not yet been validated.

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
DNS commands in its `configs/<node>-init.cfg`.

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

For automatic discovery from running devices, use the lab name:

```sh
eve securecrt palo-lab1 --credentials eve-default
eve securecrt palo-lab1 --username admin --timeout 120
```

This mode reads the live node list and native Telnet console URLs from the EVE
API, then accesses each console through EVE host SSH. It uses `CISCO_*` and
`PALO_*` credentials from `.env` for device login; it does not read DHCP leases
or use `*_MANAGEMENT_IP` variables as session targets.

- Cisco c8000v: reads `show ip interface brief`, selects assigned, up/up IPv4
  interfaces connected to a remote `pnet1` network, and reads the hostname from
  the privileged CLI prompt.
- Palo firewalls and Panorama: reads the hostname and management IPv4 address
  from `show system info`.

Sessions are named `eve/<device-hostname> - <IP>`, with one session per IP, without
prompts. `--interactive` is only available in the legacy `pnet1` mode below.
Devices must already be initialized and have Telnet consoles. Discovery does
not apply configs, change passwords, commit, or start nodes; it authenticates,
uses enable mode for Cisco, and disables CLI paging to read status. Close other
console sessions first. Stopped, unsupported (including Linux), uninitialized,
or inaccessible nodes are reported in `skipped`. If none can be discovered,
the existing output script is left unchanged. Discovery does not verify that
SSH is enabled or reachable at the returned address. This mode has automated
test coverage; live console discovery has not yet been verified.

`--credentials` and `--username` select the generated SecureCRT session login;
they do not override the `.env` credentials used to read device consoles.
The generated script still needs to be run inside SecureCRT to create/update
sessions. It uses the same output path and existing-session behavior described below.

For the existing DHCP and `.env` discovery mode, use `pnet1`:

```sh
eve securecrt pnet1
eve securecrt pnet1 --username admin
eve securecrt pnet1 --interactive --username admin
eve securecrt pnet1 --interactive --credentials eve-default
eve securecrt pnet1 --username admin --port 22 --output .state/securecrt-eve.py
```

Fetches the DHCP report over SSH, adds variables ending in `_MANAGEMENT_IP`
from `.env`, and writes `.state/securecrt-eve.py` by default. For example:

```dotenv
PANORAMA_MANAGEMENT_IP=172.16.1.99
PA_A_MANAGEMENT_IP=172.16.1.120
```

These add `eve/PANORAMA - 172.16.1.99` and `eve/PA_A - 172.16.1.120`.
The variable prefix supplies the hostname, preserving its case and underscores.
Interactive mode prompts for these hosts too, with that prefix as the default.
Shell environment values override `.env`; blank values are ignored and invalid
IPv4 addresses abort without replacing the output file. Entries are deduplicated
by IP: an environment entry takes precedence over the DHCP hostname; if multiple
environment variables share an IP, the last variable in alphabetical order wins.
These variables add session targets, not device network configuration.
An optional `PANORAMA_MANAGEMENT_IP` here is only a SecureCRT session target;
Panorama init reads its network configuration from its `.cfg` file.
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

Only active and permanent DHCP lease records are included, plus configured
management IPs regardless of DHCP leases, with one session per IP.
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
EVE_PASSWORD=your-api-password
EVE_SSH_USERNAME=root
EVE_SSH_PASSWORD=your-ssh-password
```

Replace these example values with your own credentials. After changing the SSH password,
update `EVE_SSH_PASSWORD` in `.env`; the next DHCP command uses the new value.
Shell environment variables override `.env`. Enter values without surrounding
quotes. `config/servers.yaml` maps `ssh_username_env` and `ssh_password_env` to
these variable names. DHCP commands require only the SSH credentials. Lab
commands use web/API credentials; applying init or building bootstrap media also
requires EVE host SSH credentials.

SSH password authentication is automatic, without an interactive prompt or SSH
agent/key authentication. Passwords are not passed as command-line arguments.
The host key must already be trusted in `~/.ssh/known_hosts`; for a new server,
connect with `ssh root@'<eve-host>'` and verify its fingerprint before accepting it.
The DHCP helper requires root access.

Use `--server <name>`
to select a server. Only the inspected, dedicated pnet1 dnsmasq configuration is
supported; unexpected service/configuration layouts are rejected.

### Replace the saved SSH key after rebuilding the EVE VM

When you rebuild or replace your EVE VM, its SSH host key may change.
After confirming the replacement was intentional, run these commands on your
workstation, replacing `<eve-host>` with the configured SSH IP or hostname:

```sh
ssh-keygen -R '<eve-host>'
ssh root@'<eve-host>'
```

The first command removes the old saved key. The second prompts you to trust the
new key; verify its fingerprint before accepting it, then enter the EVE host's SSH
password. Exit the SSH session, then
retry your command, for example:

```sh
eve init palo-lab --node c8kv-0
```

Repeat these steps after future intentional VM replacements. Update
`config/servers.yaml` if the address changes, and `.env` if credentials change.

### Deployment errors

For `Failed to create network (11)` during start, the CLI allows at least three
attempts, or one per distinct attached network plus a final attempt, capped at
16 attempts. It waits 1 second after the first error, 2 seconds after the second,
and 3 seconds after later errors, checking live state each time, including after
the final error. It stops retrying once the node is running. Other API errors are
not retried. Retry progress and recovery warnings appear on stderr.

On the inspected host, EVE created bridges but failed while setting
`group_fwd_mask=65535`; later attempts reused those partially created bridges.
A node with several attached networks can therefore need more than three attempts.
Retries do not repair bridge forwarding or prove that all Layer-2 protocols work.
For recurring errors inspect `/opt/unetlab/data/Logs/unl_wrapper.txt` and the host
kernel/bridge settings, even if nodes eventually start.

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
`config/servers.yaml`; it defaults to the entry named `default`, using the URL you configured. `--root <path>`
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
