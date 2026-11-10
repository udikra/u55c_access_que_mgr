# q55: U55C board access queue manager

Several users share the Alveo U55C card in the lab machine (enics-ThinkStation-P5).
Only one bitstream can be loaded at a time, and only one user can use it at a time
and get predictable results. Loading a bitstream needs the sudo-enabled `enics` account.

`q55` solves this with a queue manager that runs continuously under the `enics` account.
Users run `q55` from their own accounts, which have no sudo rights.

## Commands

```
q55 prog   <bitstream_path> [-t <minutes>] [-pci]       queue a bitstream load
q55 access <bitstream_path> [-t <minutes>] [-pci]       queue an access request (exclusive board access)
q55 status                                               queue status
q55 release                                              end your turn: access, allocation window, waiting requests
q55 shutdown                                             graceful manager shutdown (enics only)
q55 server                                               run the queue manager (enics only)
```

Every user command returns right away and never holds the terminal. The requests stay queued in the manager.

### q55 prog

Queues a load of the bitstream and returns right away. The command doesn't wait for the load.

- `-t` is the allocation window (default 10 minutes), counted from the moment the bitstream is loaded.
  - While the window is open, other users' bitstreams can't replace this one. You can replace your own
    bitstream at any time, as long as no other user holds the board on it or waits for it.
  - After the window closes, the bitstream stays loaded until another load is requested.
- The loaded bitstream is available to every user who gives the same path.
- If you ask for a different bitstream while another one is inside its window, you get a
  `BUSY` message with the queue status. Your request stays queued and loads when its turn comes.
- If the bitstream is already loaded (`ACTIVE`), being loaded (`LOADING`), or queued (`PENDING`),
  nothing new is queued and you are told which of these applies.
- A bitstream rebuilt at the same path (different size or time stamp) counts as a new load.
- The manager copies the file when the request is queued. Rebuilding it afterwards doesn't
  affect the queued load (except a rebuild while it is still pending, which replaces the queued copy).

### q55 access

Queues an access request in the manager and returns right away. When your turn comes, the manager
grants you exclusive use of the board. There is no session process and no special shell: while you hold the
access, you use the board in any way, from any of your terminals on the lab machine.

- `q55 access` answers `GRANTED` if the board was free and your bitstream is loaded, or `QUEUED` with your
  position in the access queue. A repeated request for the same bitstream isn't queued twice.
- Run `q55 status` to see when you hold the access: your name is shown as `Access holder`.
- If the bitstream isn't active or queued, `access` first queues a load (the same as `q55 prog`, with the same `-t`).
  Loading a bitstream can take a minute or two. If the load fails, the access requests for that bitstream are
  dropped, and `q55 status` shows it under `Recent events`.
- `-t` is your minimum access time (default 10 minutes), counted from when access is granted.
- When it runs out:
  - **Nobody else is waiting:** you keep the board as long as you like.
  - **Someone else is waiting:** either another user wants this bitstream, or a load of a
    different bitstream is due because the allocation window is over. 60 seconds later your access ends.
    `q55 status` shows `revoked in m:ss` next to the holder during these 60 seconds.
- `q55` never runs, watches or stops your programs. When your access ends, stop using the board.
- `q55 release` ends your access, or cancels a request that is still waiting.

Access is enforced only for users who go through `q55`. The board and its devices are
not locked, so everybody must use `q55 access`, and use the board only while `q55 status`
shows them as the access holder, to get predictable results.

### q55 release

Means "my turn is over". Run it from any terminal, even if your `-t` time isn't over.

- Your board access ends at once, with no 60-second wait, and the board goes to the next user.
- The allocation window of the bitstream you loaded ends, so other users' pending loads can go ahead.
  This is skipped if another user waits for that bitstream. The bitstream stays loaded until another load is requested.
- Your access requests that are still waiting are cancelled.
- `enics` can run `q55 release` to end anyone's access, for example one a user forgot to release.

### -pci: bitstreams that use the PCIe interface

Before every load, the manager takes the card's PCIe link down on purpose (an unexpected link drop during
programming hangs the lab machine). By default the link stays down after the load, which is right for
bitstreams that use only JTAG.

Add `-pci` to `q55 prog` or `q55 access` if your bitstream has a PCIe core (for example XDMA) and you use it from the host.
After the JTAG load, the manager then:

1. turns off runtime power management on the slot's bridge and clears Link Disable,
2. waits up to 10 seconds for the link (`DLActive`). If it doesn't come up, the link is disabled again and the load is reported as failed,
3. rescans the card's PCIe bus, so the card enumerates again and the XDMA driver binds to it,
4. gives the requesting user read/write access to the card's PCIe devices: the BAR files of every card function
   (`/sys/bus/pci/devices/0000:4e:00.*/resourceN` and `resourceN_wc`, by owner) and `/dev/xdma*` (by ACL).

The manager log shows the `PCIe:` lines of the bring-up (link status, functions found, permissions).

- The XDMA driver must already be loaded (`lsmod | grep xdma`). Without it, the link comes up and the BAR files
  are usable, but there are no `/dev/xdma*` devices. The load output in the manager log warns about this.
- The PCIe devices follow the access holder. When another user is granted access on a `-pci` bitstream, the manager
  gives the devices to that user. The previous user's permissions are removed. Every load recreates
  the devices with root-only permissions.
- A `-pci` request for a bitstream that is loaded without the PCIe link counts as a new load (it is reloaded with `-pci`).
  A request without `-pci` is served by a `-pci` load of the same file.
- `q55 status` shows `(PCIe link up)` next to the active bitstream, and `-pci` next to queued requests.

### Scheduling rules

1. A pending load waits until the active bitstream's allocation window is over, and until
   the current board access is released or revoked (after its minimum time and 60 more seconds).
   Exception: the user who loaded the active bitstream can replace it before its window is over, as long as
   no other user holds the board on that bitstream or waits for it.
2. Once the window is over, pending loads come before new access grants on the old bitstream.
   Users still waiting for the old bitstream have it queued again behind the pending loads.
3. Access requests are served first come, first served among the users of the loaded bitstream.
4. If a load fails, the board state is unknown. Users waiting for that bitstream get the
   error, and the failure shows in `q55 status`.

## Installation (once, on the lab machine, as enics)

### Prerequisites

These were checked on enics-ThinkStation-P5:

- Python 3.9 or newer (the machine has 3.12). `q55.py` uses only the standard library.
- Vivado Lab at `/tools/Xilinx/2026.1/Vivado_Lab` (the path is set in `program.sh`).
- The card's JTAG cable (FT4232H) is connected.
- The `enics` account has sudo rights.
- For `-pci`: `setfacl` (package `acl`) and the XDMA driver, loaded by the lab's own setup.

### Where the files live

| Location | Owner | Purpose |
|----------|-------|---------|
| `/home/enics/u55c_server` | enics | enics's working copy of this repository. Updates arrive here first. Other users can't read `/home/enics`, and it stays that way. |
| `/opt/q55` | root | The installed copy that everyone runs, readable by all users. It is copied from `/home/enics/u55c_server`. |
| `/usr/local/bin/q55` | root | Symlink to `/opt/q55/q55`, so `q55` is on every user's `PATH`. |

Always run the tool from `/opt/q55`, even as enics: other users can't reach `/home/enics/u55c_server`.

### Step 1: get the repository into /home/enics/u55c_server (as enics)

Use either option.

Option A: clone from GitHub. The enics account needs read access to the repository,
for example through a GitHub deploy key, or HTTPS with a token.

```
su - enics
git clone git@github.com:udikra/u55c_access_que_mgr.git ~/u55c_server
```

Option B: copy from a Windows checkout. enics has no SSH key on the lab machine, so do it in two steps.
First copy the files, under your own lab account, to your workspace `/home/enics/users_workspace/$USER`
(any folder enics can read works). Then copy them into place as enics:

```
# on Windows (Git Bash), in the repository folder. LAB_USER is your account on the lab machine:
LAB_USER=<your lab account>
ssh $LAB_USER@<lab-machine> mkdir -p /home/enics/users_workspace/$LAB_USER/q55_files
scp q55 q55.py program.sh jtag_program.tcl README.md $LAB_USER@<lab-machine>:/home/enics/users_workspace/$LAB_USER/q55_files/

# on the lab machine, logged in as your own account:
su - enics -c "mkdir -p ~/u55c_server && cp /home/enics/users_workspace/$USER/q55_files/* ~/u55c_server/"
```

The double quotes matter: `$USER` is replaced with your account name before `su` switches to enics.
After `su - enics`, `$USER` is `enics`.

### Step 2: install from /home/enics/u55c_server into /opt/q55 (as enics)

```
cd ~/u55c_server
sudo install -d -m 755 /opt/q55
sudo install -m 755 q55 q55.py program.sh /opt/q55/
sudo install -m 644 jtag_program.tcl /opt/q55/
sudo sed -i 's/\r$//' /opt/q55/*                  # strip Windows line endings, if any
sudo ln -sf /opt/q55/q55 /usr/local/bin/q55
```

### Step 3: check the installation

```
which q55                 # /usr/local/bin/q55
q55 -h                    # usage text, from any account
q55 status                # "the U55C queue manager is not running" until the manager is started (see below)
```

### Updating to a new version

1. Stop the manager with `q55 shutdown`, as enics.
2. Update `/home/enics/u55c_server`: run `cd ~/u55c_server && git pull` (Option A), or copy the new files again (Option B).
3. Repeat Step 2. The `install` command overwrites the old files in `/opt/q55`.
4. Start the manager again (see below). The active bitstream is restored from `~/.q55_state.json`.

### Uninstalling

```
sudo rm -rf /opt/q55 /usr/local/bin/q55
rm -rf ~/u55c_server ~/.q55_state.json ~/.q55.log        # as enics
```

## Running the manager (enics)

The manager must run as `enics`, and only one manager can run at a time. Start it in a terminal that
stays open after you log out. Use tmux so it survives a disconnect:

```
tmux new -s q55           # or attach to an existing session: tmux attach -t shared
su - enics                # skip this if you are already enics
q55 server                # enter the enics password when sudo asks
```

Detach with `Ctrl-b d`. The manager keeps running. To see it again, run `tmux attach -t q55`.

After a reboot of the lab machine, start the manager again: it isn't started automatically.

- It asks for the enics sudo password once, then keeps the sudo credential alive (`sudo -n -v` every 4 minutes).
- The terminal shows the manager log. The log is also kept in `~/.q55.log`.
- The loaded bitstream is saved in `~/.q55_state.json` and restored after a restart.
  It is shown as not verified, because someone may have loaded the board by hand in the meantime.
- Stop it with `q55 shutdown` (from enics) or Ctrl-C. Both are graceful: the manager waits
  for a load in progress, and the access holder keeps the board for 60 more seconds. Waiting access requests
  are dropped. A second Ctrl-C exits at once.

If the manager loses its sudo credential, loads fail with a clear message. In that case restart
the manager. Alternatively, a sudoers `NOPASSWD` rule for `setpci` and `tee` (and for `-pci`: `chown`, `chmod`, `setfacl`)
removes the need for the password.

## Using q55 (all users)

You don't need to install anything. `q55` is in `/usr/local/bin`, which is already in every user's `PATH`.
If `q55` isn't found, run `/opt/q55/q55` or add `/usr/local/bin` to your `PATH`.

Typical use, from any terminal of your own account on the lab machine:

```
q55 status                                           # is the manager running, and what is loaded?
q55 access <path_to_your_bitstream_file> -t 20      # queue an access request, load if needed
q55 status                                           # check that you are the "Access holder"
<your own commands>                                  # use the board, from any of your terminals
q55 release                                          # release the board for the next user
```

While you hold the access you can run any program, in any way, from any terminal: Python scripts,
terminal programs, your own test environment, and so on. `q55` doesn't care what you run. It only
makes sure nobody else is granted the board while you hold it.

Don't load bitstreams yourself (Vivado Hardware Manager, `program.sh`) while you hold the access. The manager wouldn't
know about the load, so `q55 status` would show the wrong bitstream and the next user would get
an unexpected image. Always load through `q55 prog` or `q55 access`.

The bitstream must be a file you can read. Its path doesn't need to be readable by `enics`.
If you see `the U55C queue manager is not running`, ask whoever runs the enics account to start `q55 server`.

## Files

| File | Purpose |
|------|---------|
| `q55` | The command users run. A short bash wrapper that finds its real location (following the `/usr/local/bin/q55` symlink) and runs `q55.py` from there. See below. |
| `q55.py` | Queue manager and client (Python 3 standard library only) |
| `program.sh` | JTAG load with PCIe link disable. `--pcie USER` adds the link bring-up after the load; `--pcie-grant USER` and `--pcie-up USER` only hand over the devices or bring the link up (see How it works). `--pcie-only` only takes the link down. Based on u55c_py_access, with `sudo -n` |
| `jtag_program.tcl` | Vivado Lab JTAG programming |

Why `q55` is a wrapper and not a direct link to `q55.py`: the manager finds `program.sh` next to `q55.py`
(`os.path.abspath(__file__)`). Python doesn't resolve symlinks there, so if `/usr/local/bin/q55` pointed straight
at `q55.py`, it would look for `/usr/local/bin/program.sh`. The wrapper runs `readlink -f` first, so the
symlink works. It uses `exec`, so no extra process stays between the user's terminal and `q55.py`,
and the exit code reaches the terminal directly.

## How it works

- **Communication:** the manager listens on the Unix socket `/tmp/q55/q55.sock` (mode 666).
  The kernel reports who each client is (SO_PEERCRED), so users can't pose as someone else.
- **Trust:** clients only talk to a socket directory owned by `enics`.
- **Bitstream files:** the client opens the bitstream file and passes the open file to the
  manager (SCM_RIGHTS). The manager copies it to its private spool `/tmp/q55/spool`. This
  works even though the manager can't read users' home directories.
- **Loading:** `program.sh` disables the card's PCIe link, then loads the bitstream with
  Vivado Lab (`/tools/Xilinx/2026.1/Vivado_Lab`). For `-pci` requests the manager runs
  `program.sh <bit> --pcie <user>`, which brings the link up again, and on a later access grant
  `program.sh --pcie-grant <user>`, which only changes the device permissions.
  For recovery, enics can run `program.sh --pcie-up <user>` by hand: it brings the link up and gives
  the devices to the user without loading anything. Do it only when no load is running, since the manager doesn't know about it.
- **Access:** the access queue and the access holder live only in the manager. A client sends one
  request, gets one answer and disconnects. Ending an access only changes the manager's state, and
  user processes are never signalled. The access state isn't saved, so a manager restart drops it.
- **Test overrides (environment variables):** `Q55_DIR`, `Q55_MGR_USER`, `Q55_PROG_CMD`
  (replaces program.sh, no sudo, called with the same arguments), `Q55_GRACE_SEC`, `Q55_STATE`, `Q55_LOG`.
