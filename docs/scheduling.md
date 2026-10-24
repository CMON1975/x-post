# Scheduling

`--at WHEN` queues a post instead of sending it now:

```
x-post --at 'tomorrow 09:00' 'good morning'
scheduled 3f2a for Sat 2026-10-24 09:00
```

## When

Times are local.

| WHEN | Means |
| --- | --- |
| `+90m`, `+2h`, `+1d` | that long from now |
| `09:00` | the next 09:00: today's, or tomorrow's once today's has passed |
| `tomorrow 09:00` | tomorrow at 09:00 |
| `fri 18:00` | the next Friday at 18:00, a week out if it's Friday after 18:00 |
| `2026-10-24 18:30` | that date and time |

A time already past is refused. Offsets count on the wall clock, so `+2h` across a daylight-saving change is an hour off.

## What can be scheduled

Everything: `-e` threads and their `image:` lines, `-i` and `-s` images, `--alt` and `--poll`. Every check and question happens when you schedule (length, images, typos, the link price, the [budget](costs.md#monthly-budget)), so nothing asks anything when the post goes out. Images are copied into the queue, so moving or deleting the originals afterwards is fine. `--check --at` also says when the post would go out.

## The queue

| Command | Does |
| --- | --- |
| `x-post --scheduled` | lists the queue, soonest first, with what each post will bill |
| `x-post --cancel ID` | takes a post out |
| `x-post --move ID WHEN` | changes when it goes out |
| `x-post --revise ID` | opens it in your editor to rewrite |
| `x-post --retry ID` | releases a [held](#held-posts) post |

`--scheduled`, `--cancel` and `--retry` need no credentials.

```
x-post --scheduled
b7e2  Fri 2026-10-23 08:00  $0.015  'good morning'
      held: 403 Forbidden: Duplicate content.
3f2a  Fri 2026-10-23 09:30  $0.020  'with a picture', 1 image
9c01  Fri 2026-10-23 18:00  $0.215  'a thread' and 2 more posts
x-post --cancel b7e2
cancelled b7e2: 'good morning'
```

### Moving

`x-post --move 3f2a 'tomorrow 09:00'` reads the time as `--at` does. A held post stays held, so `--move` then `--retry` sends it at the new time.

With a [budget](costs.md#monthly-budget) set, a move into another month is checked against that month, and off a terminal it's refused (`…, past your $1.00 budget; not moved`). A move within the month isn't checked, since the month's total stays the same.

### Revising

`x-post --revise ID` opens a queued post in your editor, laid out as in `-e`: `---` between a thread's posts, `image:` and `alt:` lines naming the images' copies in the queue, and a `reply-to:` first line on a held thread waiting to carry on. Move, add or drop images as you would in `-e`.

- Saving runs the same checks and questions as `--at`. A poll stays as it was.
- The post is locked while the editor is open, so it can't go out mid-edit. One that comes due meanwhile goes out at the next run after you save.
- An edit that fails a check, or whose link price you decline, is kept, and the next `--revise` reopens it. Saving it empty drops the edit and leaves the post as it was.

## Held posts

A queued post goes out however late it is, if the machine was off at the time, say. If X refuses it, it's **held** with X's reason instead of being retried every minute. A thread that stops partway is held as its unsent rest, replying to the last post sent.

When the cause has passed (X was down, the network dropped), release it with `--retry`, and it goes out at the next run. A thread carries on where it stopped:

```
x-post --retry b7e2
b7e2 goes out within a minute: 'good morning'
```

If it never will go out (duplicate content, say), `--cancel` it. Until you do one or the other, every other x-post run (posting, `-e`, `--check`, `--at`, `--undo`, `--delete`) starts with a reminder:

```
x-post: 1 scheduled post is held; x-post --scheduled shows why
```

### Alerts

To hear about a hold as it happens, set `alert` in `~/.config/x-post/config.toml` to a command. x-post runs it with a title and a body added after it, and expands a `~` at its start:

```toml
alert = "notify-send --urgency=critical"
```

A script of your own there can route it however you like: a desktop notification at the desk, a phone push when you're away. Without the setting there's no alert, only the journal line and the reminder. If the alert can't go out, the journal says so and the post is held all the same.

## The timer

You never send queued posts yourself. The first `--at` installs a systemd user timer, `x-post-due.timer` in `~/.config/systemd/user/`, which runs `x-post --send-due` every minute.

- A post goes out within a minute of its time, or within a minute of boot if the machine was off.
- Posts only go out while you're logged in, unless lingering is on (`loginctl enable-linger`).
- Each later `--at` makes sure the timer is enabled and running, and updates its units if a newer x-post changed them. If the timer can't be started (no user session bus, say), the post isn't queued.
- While nothing is queued, the service is skipped without starting x-post.
- `journalctl --user -u x-post-due` shows each post sent and any failure, without systemd's per-minute lines.

The queue lives in `~/.local/state/x-post/scheduled/`, one folder per post. Each post has a lock: while the timer is sending one, `--cancel` and `--retry` say to try again in a moment, and the timer leaves a post you're changing for its next run.
