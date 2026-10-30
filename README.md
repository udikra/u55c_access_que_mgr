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

Other users can't read `/home/enics`, so install under `/opt`:

```
# from a clone (or copy) of this repository on the lab machine:
cd u55c_access_que_mgr
sudo mkdir -p /opt/q55
sudo cp q55 q55.py program.sh jtag_program.tcl /opt/q55
sudo chmod 755 /opt/q55/q55 /opt/q55/q55.py /opt/q55/program.sh
sudo ln -sf /opt/q55/q55 /usr/local/bin/q55
```

Files copied from Windows need `sed -i 's/\r$//' /opt/q55/*`.

## Running the manager (enics)

Start it in a terminal that stays open, for example the tmux session `shared`:

```
q55 server
```

- It asks for the enics sudo password once, then keeps the sudo credential alive (`sudo -n -v` every 4 minutes).
- The terminal shows the manager log. The log is also kept in `~/.q55.log`.
- The loaded bitstream is saved in `~/.q55_state.json` and restored after a restart.
  It is shown as not verified, because someone may have loaded the board by hand in the meantime.
- Stop it with `q55 shutdown` (from enics) or Ctrl-C. Both are graceful: the manager waits
  for a load in progress, and the access holder gets the 60-second warning. A second Ctrl-C exits at once.

If the manager loses its sudo credential, loads fail with a clear message. In that case restart
the manager. Alternatively, a sudoers `NOPASSWD` rule for `setpci` and `tee` removes the need for the password.

## Files

| File | Purpose |
|------|---------|
| `q55` | Wrapper script (`python3 q55.py`) |
| `q55.py` | Queue manager and client (Python 3 standard library only) |
| `program.sh` | JTAG load with PCIe link disable. Copied from u55c_py_access, with `sudo -n` |
| `jtag_program.tcl` | Vivado Lab JTAG programming |

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
