# q55 user guide

The Alveo U55C card in the lab machine (enics-ThinkStation-P5) is shared, so only one bitstream is
loaded at a time and only one user works with the board at a time. `q55` queues users and bitstream
loads, so you never need sudo or the `enics` account.

Run `q55` from your own account. Nothing needs to be installed.

## Quick start

```
q55 status                                         # what is loaded, who holds the board, who is waiting
q55 access <path_to_your_bitstream_file> -t 20     # queue an access request, load the bitstream if needed
<your own commands>                                # use the board until your -t minutes are over
q55 release                                        # done early: release the board for the next user
```

## Commands

| Command | What it does |
|---------|--------------|
| `q55 status` | Shows the loaded bitstream, the current board holder, the queues and recent events. |
| `q55 access <bit> [-t <min>]` | Queues an access request. When your turn comes, you get exclusive use of the board in a new shell. Loads `<bit>` first if needed. |
| `q55 access <bit> [-t <min>] -- <cmd>` | The same, but runs `<cmd>` and releases the board when it ends. |
| `q55 prog <bit> [-t <min>]` | Only queues a load of `<bit>` and returns right away. It doesn't give you the board. |
| `q55 release` | Ends your turn now, even before your `-t` is over: releases your board access, ends the allocation window of your bitstream, and cancels your waiting access requests. |
| `q55 -h` | Help. |

`-t` defaults to 10 minutes.

## Working with the board (`q55 access`)

1. **Wait.** If the board is busy, you are queued and see your position. Ctrl-C cancels.
2. **Load.** If your bitstream isn't loaded, it is loaded for you. This can take a minute or two.
3. **Use the board.** You get a new shell in the same directory, with the prompt marked `(q55)`.
   You hold the board as long as that shell is open. Run anything you like there: Python scripts,
   terminal programs, your own test setup, and so on.
4. **Release.** There are two ways:
   - `exit`: ends the session, and the next user gets the board. Your bitstream stays protected for the
     rest of its allocation window, so you can come back to it.
   - `q55 release`: you are done with your turn. It works from inside the session or from any other terminal.
     It ends your session at once, ends your bitstream's allocation window so others can load theirs,
     and cancels your access requests that are still waiting.

With `-- <cmd>`, steps 3 and 4 happen by themselves: the board is released when `<cmd>` ends.

Inside the session these environment variables are set (using them is optional):

| Variable | Value |
|----------|-------|
| `Q55_BITSTREAM` | the full path of the loaded bitstream |
| `Q55_ACCESS` | `1`, so your scripts can check that they run inside a session |

## How long you keep the board

- `-t` is your **minimum** time, counted from when the board is granted.
- After that, you keep the board for as long as nobody else is waiting.
- If someone is waiting after your minimum time, you get a warning, and **60 seconds later your
  shell or command is ended**. Save your work when you see the warning. `q55` then exits with code 2.

## Bitstreams

- Pass the path to your own `.bit` file. A copy is taken when the request is queued, so you can rebuild
  right after. A rebuilt file (new size or time stamp) counts as a new bitstream.
- Users who give the same path share the loaded bitstream, with no reload.
- After a load, the bitstream is protected for its `-t` minutes (the allocation window), and other users
  can't replace it during that time. After that, it stays loaded until someone asks for a different one.
- You can replace a bitstream you loaded yourself at any time, even inside its window, as long as
  no other user is using it or waiting for it. So switching between your own bitstreams doesn't wait for the window.

Messages you may see when you ask for a bitstream:

| Message | Meaning |
|---------|---------|
| `QUEUED ... loading starts now` | Your load starts right away. |
| `BUSY` | Another bitstream is still inside its window. Your load is queued, and the status follows. |
| `ACTIVE` | It is already loaded, so nothing is reloaded. |
| `LOADING` / `PENDING` | It is being loaded now, or is already in the load queue. |

## Rules

- **Always go through `q55 access`.** The board and its devices aren't locked. Anyone who uses them
  outside a session disturbs whoever holds the board.
- **Don't load bitstreams yourself** (Vivado Hardware Manager, `program.sh`). The queue manager wouldn't
  know about the load, and the next user would get the wrong image. Using Vivado for debugging without
  loading a bitstream is fine.
- Run `q55 release` as soon as you are done with your turn.

## Troubleshooting

| Problem | What to do |
|---------|------------|
| `the U55C queue manager is not running` | Ask whoever runs the enics account to start `q55 server`. |
| `bitstream ... not found` | Check the path. It must be a file you can read. |
| `load of ... failed` | The board state is unknown. Check `q55 status`, and report it if a load that worked before now fails. |
| `q55: command not found` | Use `/opt/q55/q55`, or add `/usr/local/bin` to your `PATH`. |
