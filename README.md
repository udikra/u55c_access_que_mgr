# q55: U55C board access queue manager

Several users share the Alveo U55C card in the lab machine (enics-ThinkStation-P5).
Only one bitstream can be loaded at a time, and only one user can use it at a time
and get predictable results. Loading a bitstream needs the sudo-enabled `enics` account.

`q55` solves this with a queue manager that runs continuously under the `enics` account.
Users run `q55` from their own accounts, which have no sudo rights.

## Commands

```
q55 prog   <bitstream_path> [-t <minutes>]               queue a bitstream load
q55 access <bitstream_path> [-t <minutes>] [-- <cmd>]    exclusive board access
q55 status                                               queue status
q55 shutdown                                             graceful manager shutdown (enics only)
q55 server                                               run the queue manager (enics only)
```

### q55 prog

Queues a load of the bitstream and returns right away. The command doesn't wait for the load.

- `-t` is the allocation window (default 10 minutes), counted from the moment the bitstream is loaded.
  - While the window is open, other bitstreams can't replace this one.
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

Waits for exclusive use of the board, then runs `<cmd>` or, if you give no command, an
interactive shell. The board is released when the command or shell exits.

- If the bitstream isn't active or queued, `access` first queues a load (the same as `q55 prog`, with the same `-t`).
- `-t` is your minimum access time (default 10 minutes), counted from when access is granted.
- When it runs out:
  - **Nobody else is waiting:** you keep the board as long as you like.
  - **Someone else is waiting:** either another user wants this bitstream, or a load of a
    different bitstream is due because the allocation window is over. You get a warning, and
    60 seconds later your command or shell is ended with SIGHUP, then SIGKILL.
- Ctrl-C while waiting cancels the request.
- Inside the session these variables are set:
  - `Q55_ACCESS=1`
  - `Q55_BITSTREAM=<path>`
  - `Q55_UART=/dev/ttyUSB2` (the card's UART, FT4232H channel C)

Examples:

```
q55 access ~/proj/build/my_design.bit -t 20                      # shell
q55 access ~/proj/build/my_design.bit -- python3 my_test.py      # one command
```

Access is enforced only for users who go through `q55`. The UART device and the board itself
are not locked, so everybody must use `q55 access` to get predictable results.

### Scheduling rules

1. A pending load waits until the active bitstream's allocation window is over, and until
   the current access session is released or revoked (after its minimum time and the 60-second warning).
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
- The card's JTAG/UART cable (FT4232H) is connected, and its UART is `/dev/ttyUSB2`.
- The `enics` account has sudo rights.

### Step 1: copy the repository files to the lab machine

You need the four files `q55`, `q55.py`, `program.sh` and `jtag_program.tcl`. Use either option.

Option A: clone from GitHub on the lab machine. This needs an account with access to the repository.

```
cd /tmp
git clone git@github.com:udikra/u55c_access_que_mgr.git
```

Option B: copy from a Windows checkout with `scp`. Use an account that has an SSH key on the lab machine, for example `udi`:

```
scp q55 q55.py program.sh jtag_program.tcl udi@<lab-machine>:/tmp/u55c_access_que_mgr/
```

Create the target directory first with `ssh udi@<lab-machine> mkdir -p /tmp/u55c_access_que_mgr`.

### Step 2: install under /opt/q55 (as enics)

Other users can't read `/home/enics`, so the tool goes under `/opt`, where everyone can read it:

```
su - enics
cd /tmp/u55c_access_que_mgr
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
q55 status                # "the U55C queue manager is not running" until Step 4
```

### Updating to a new version

1. Stop the manager with `q55 shutdown`, as enics.
2. Repeat Steps 1 and 2. The `install` command overwrites the old files.
3. Start the manager again (see below). The active bitstream is restored from `~/.q55_state.json`.

### Uninstalling

```
sudo rm -rf /opt/q55 /usr/local/bin/q55
rm -f ~/.q55_state.json ~/.q55.log        # as enics
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
  for a load in progress, and the access holder gets the 60-second warning. A second Ctrl-C exits at once.

If the manager loses its sudo credential, loads fail with a clear message. In that case restart
the manager. Alternatively, a sudoers `NOPASSWD` rule for `setpci` and `tee` removes the need for the password.

## Using q55 (all users)

You don't need to install anything. `q55` is in `/usr/local/bin`, which is already in every user's `PATH`.
If `q55` isn't found, run `/opt/q55/q55` or add `/usr/local/bin` to your `PATH`.

Typical session from your own account:

```
q55 status                                           # is the manager running, and what is loaded?
q55 access ~/proj/build/my_design.bit -t 20          # wait for the board, load if needed, open a shell
python3 my_test.py --port $Q55_UART                  # inside the shell: use the board
exit                                                 # release the board for the next user
```

Or run one command and release the board when it ends:

```
q55 access ~/proj/build/my_design.bit -- python3 my_test.py
```

The bitstream must be a file you can read. Its path doesn't need to be readable by `enics`.
If you see `the U55C queue manager is not running`, ask whoever runs the enics account to start `q55 server`.

## Files

| File | Purpose |
|------|---------|
| `q55` | The command users run. A short bash wrapper that finds its real location (following the `/usr/local/bin/q55` symlink) and runs `q55.py` from there. See below. |
| `q55.py` | Queue manager and client (Python 3 standard library only) |
| `program.sh` | JTAG load with PCIe link disable. Copied from u55c_py_access, with `sudo -n` |
| `jtag_program.tcl` | Vivado Lab JTAG programming |

Why `q55` is a wrapper and not a direct link to `q55.py`: the manager finds `program.sh` next to `q55.py`
(`os.path.abspath(__file__)`). Python doesn't resolve symlinks there, so if `/usr/local/bin/q55` pointed straight
at `q55.py`, it would look for `/usr/local/bin/program.sh`. The wrapper runs `readlink -f` first, so the
symlink works. It uses `exec`, so no extra process stays between the user's terminal and `q55.py`.
Signals, the terminal foreground and the exit code reach `q55.py` directly.

## How it works

- **Communication:** the manager listens on the Unix socket `/tmp/q55/q55.sock` (mode 666).
  The kernel reports who each client is (SO_PEERCRED), so users can't pose as someone else.
- **Trust:** clients only talk to a socket directory owned by `enics`.
- **Bitstream files:** the client opens the bitstream file and passes the open file to the
  manager (SCM_RIGHTS). The manager copies it to its private spool `/tmp/q55/spool`. This
  works even though the manager can't read users' home directories.
- **Loading:** `program.sh` disables the card's PCIe link, then loads the bitstream with
  Vivado Lab (`/tools/Xilinx/2026.1/Vivado_Lab`).
- **Revoking:** an access session runs in its own process group in the terminal foreground,
  so it can be ended as a whole.
- **Test overrides (environment variables):** `Q55_DIR`, `Q55_MGR_USER`, `Q55_PROG_CMD`
  (replaces program.sh, no sudo), `Q55_GRACE_SEC`, `Q55_STATE`, `Q55_LOG`.
