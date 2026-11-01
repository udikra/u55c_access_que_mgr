# q55 user guide

The Alveo U55C card in the lab machine (enics-ThinkStation-P5) is shared, so only one bitstream is
loaded at a time and only one user works with the board at a time. `q55` queues users and bitstream
loads, so you never need sudo or the `enics` account.

Run `q55` from your own account, in any terminal on enics-ThinkStation-P5. Nothing needs to be installed.
Every `q55` command returns right away, so it never holds your terminal.

## Quick start

```
q55 status                                         # what is loaded, who holds the board, who is waiting
q55 access <path_to_your_bitstream_file> -t 20     # queue an access request, load the bitstream if needed
q55 status                                         # check that you are the "Access holder"
<your own commands>                                # use the board, from any of your terminals on the machine
q55 release                                        # done: release the board for the next user
```

## Commands

| Command | What it does |
|---------|--------------|
| `q55 status` | Shows the loaded bitstream, the current board holder, the queues and recent events. |
| `q55 access <bit> [-t <min>]` | Queues an access request and returns. When your turn comes, you hold the board. Loads `<bit>` first if needed. |
| `q55 prog <bit> [-t <min>]` | Only queues a load of `<bit>`. It doesn't give you the board. |
| `q55 release` | Ends your turn now, even before your `-t` is over: releases your board access, ends the allocation window of your bitstream, and cancels your waiting access requests. |
| `q55 -h` | Help. |

`-t` defaults to 10 minutes.

## Working with the board (`q55 access`)

1. **Queue.** `q55 access` queues your request and returns at once. It tells you whether you hold the
   board already (`GRANTED`) or where you are in the access queue (`QUEUED`).
2. **Load.** If your bitstream isn't loaded, the manager loads it for you when its turn comes.
   This can take a minute or two.
3. **Wait for your turn.** Run `q55 status`. You hold the board when your name is shown as
   `Access holder`. A failed load or a dropped request also shows under `Recent events`.
4. **Use the board.** While you hold it, use the board in any way, from any of your terminals on
   enics-ThinkStation-P5: Python scripts, terminal programs, your own test setup, and so on.
   `q55` doesn't run or watch your programs.
5. **Release.** Run `q55 release`, from any terminal, when you are done. The next user gets the board,
   your bitstream's allocation window ends so others can load theirs, and your access requests that are
   still waiting are cancelled.

## How long you keep the board

- `-t` is your **minimum** time, counted from when the board is granted.
- After that, you keep the board for as long as nobody else is waiting.
- If someone is waiting after your minimum time, your access ends **60 seconds later**. `q55 status` shows
  `revoked in m:ss` next to your name during these 60 seconds.
- `q55` doesn't stop your programs when your access ends. Stop using the board yourself: from then on
  it belongs to the next user.

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

- **Always go through `q55 access`.** The board and its devices aren't locked. Use the board only while
  `q55 status` shows you as the access holder. Anyone else who uses it disturbs whoever holds the board.
- **Don't load bitstreams yourself** (Vivado Hardware Manager, `program.sh`). The queue manager wouldn't
  know about the load, and the next user would get the wrong image. Using Vivado for debugging without
  loading a bitstream is fine.
- Run `q55 release` as soon as you are done with your turn.

## Troubleshooting

| Problem | What to do |
|---------|------------|
| `the U55C queue manager is not running` | Ask whoever runs the enics account to start `q55 server`. |
| `bitstream ... not found` | Check the path. It must be a file you can read. |
| `LOAD FAILED` in `q55 status` | The board state is unknown, and access requests for that bitstream are dropped. Report it if a load that worked before now fails. |
| `q55: command not found` | Use `/opt/q55/q55`, or add `/usr/local/bin` to your `PATH`. |
