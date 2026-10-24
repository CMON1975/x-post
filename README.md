# x-post

Post to X from the terminal without opening the site or app. It only writes: nothing in it needs you to have read X first, so it doesn't quote posts, reply to other people's, or show timelines.

```
x-post 'having a great day'
https://x.com/i/status/1850000000000000001
```

Or run it with no message and type at the prompt, where nothing needs quoting:

```
x-post
post: it's #great! costs $5
https://x.com/i/status/1850000000000000001
```

It prints the new post's URL and exits 0. At the prompt, Ctrl-C or Ctrl-D cancels without posting. On failure it prints one line to stderr (the API's own reason, e.g. duplicate content or bad credentials) and exits 1.

For a post with line breaks, `x-post -e` opens your editor (`$VISUAL`, then `$EDITOR`, then the first of nvim, vim, vi, nano that's installed). Save and quit to post; quit with the file empty, or `:cq`, to cancel. The draft lives at `~/.local/state/x-post/draft.txt` until it's posted, so a post X rejects (too long, duplicate) or one you decline at the link question reopens with the next `x-post -e`.

To attach images, add `-i` once per image (up to 4, JPEG, PNG or WebP, 5 MB each), or `-i` once for a GIF (15 MB) or a video (MP4 or MOV, 512 MB), on its own, since X doesn't mix them:

```
x-post -i ~/photo.jpg -i ~/scan.png 'the view from here'
```

An image on another machine can be given as `host:path`, scp style, and is copied over ssh into a temporary folder that's deleted as soon as it's read. Paths are relative to the remote home unless they start with a drive letter, and forward slashes work on Windows:

```
x-post -i pc:Pictures/photo.jpg 'caption'
x-post -i "pc:C:/Users/you/Desktop/screen shot.png" 'caption'
```

For the screenshot you just took, `-s` attaches the newest image (PNG, JPEG or WebP) in a folder on a Windows machine you reach over ssh (it lists the folder with `dir`, so the machine's ssh shell must be `cmd.exe`). Name the machine and folder in `~/.config/x-post/config.toml`, in `-i`'s `host:path` form; without it, `-s` says so before anything else:

```toml
screenshots = "pc:Pictures/Screenshots"
```

It prints the screenshot's name first so you can see which one it picked:

```
x-post -s 'look at this'
screenshot: Screenshot 2026-10-23 065138.png
```

Any host you can ssh to without a password works. The copy never prompts and gives up after 10 seconds if the host doesn't answer. A one-letter "host" (`C:/x.png`) or a `/` before the colon (`./odd:name.png`) keeps the path local.

Add alt text, read out by screen readers, with `--alt`, once per image in order: the `-i` images first, then the `-s` screenshot. `--alt ''` skips an image, and leaving images off the end is fine. Each costs $0.005 and may be up to 1000 characters, which is checked before anything is sent:

```
x-post -i cat.png -i dog.png --alt '' --alt 'a beagle asleep on a keyboard' 'pets'
x-post -s --alt 'the X developer console, keys tab' 'found it'
```

Every image is vetted before the prompt and before anything is sent (it must exist, be under its limit, and really be one of those formats, judged by its bytes, not its name), and nothing is uploaded until the link question, if any, is answered. With images the caption can be empty. In a thread, they go with the first post (`image:` lines, below, put images on any post). `--check` vets and lists them too. Uploading isn't billed, and an image post bills like a plain one ($0.015): observed on the first live image post, though X doesn't document it.

A GIF or video goes up in 4 MB chunks, and X then processes it before a post can carry it: x-post prints `x-post: waiting for X to process clip.mp4` and polls as often as X asks, for up to 10 minutes. If X fails to process it (a codec it doesn't take, say; H.264 with AAC is the safe choice), or is still at it after that, nothing is posted. X allows video up to 8 GB and 20 minutes, but x-post stops at 512 MB because it holds the file in memory, and `--at` copies it into the queue. A video's length is read from its header before anything uploads, and one outside X's 0.5 seconds to 20 minutes is refused there (`clip.mp4 runs 21:04; X takes video from 0.5 s to 20 minutes`) instead of after the upload and processing wait. A fragmented MP4, which some recorders write and whose header gives no length, is left for X to judge. Premium accounts may post up to 125 minutes, which this check doesn't know about. A video is recognised by its MP4/MOV header, so an iPhone HEIC photo, which shares that header's shape, is refused rather than sent as video. X doesn't document alt text on GIFs and video, but it took it on both in live tests; if it ever refuses, nothing is posted. x-post doesn't check a video's dimensions: X's docs list 1280×1024 as the largest and promise 1080p only to subscribers, but a 1920×1080 clip posted fine from an account without Premium.

For a poll, add `--poll` once per option (2 to 4, up to 25 characters each). The post's text is the question, so it can't be empty:

```
x-post --poll tea --poll coffee --poll-for 6h 'tea or coffee?'
```

It stays open a day unless `--poll-for` says otherwise: `30m`, `6h`, `2d`, anything from 5 minutes to 7 days. The options and the time are checked before the prompt and before anything is sent, and `--check` shows them (`poll for 6h: 'tea', 'coffee'`). A poll can't go with images or video, since X takes one or the other. In a thread it goes with the first post; with `--at` it's queued with the post, and its clock starts when the post goes out. X's price list has no line for polls, so a poll post should bill as a plain one ($0.015).

For something too long for one post, write a thread in `x-post -e`, with a line of just `---` between posts:

```
first post
---
second post
---
third post
```

Every post is checked for length before any is sent, the link question is asked once for the whole thread, and the posts go out as a reply chain ($0.015 each), printing each URL as it lands. If one fails partway, the unsent posts stay in the draft under a first line `reply-to: https://x.com/i/status/<last one sent>`, and `x-post -e` carries on from there; delete that line to start a fresh thread instead. `---` only splits in the editor; on the command line or at the prompt it's just text. X only lets the API reply to your own posts (or to posts that mention you), which is all a thread needs.

`x-post -e --numbered` ends each post of a thread with its place: `first post 1/3`, `second post 2/3`, `third post 3/3` (a post of just images gets the mark alone, and a single post none). The marks are added as it goes out, not to your draft, and count toward each post's length, so `--check --numbered` measures them; `s` leaves room for them when it splits, two digits' worth from ten posts on. A numbered thread that stops partway keeps its marks in the draft, and `x-post -e` carries it on as it is, with `--numbered` or without: a thread whose posts already end in their places (`2/3`, `3/3`) isn't marked again. A post that merely ends in a fraction (`50/50`) is still marked. Under `--at` the marks are queued with the text, and `--revise` opens a numbered thread without them and numbers it again on saving, so adding or dropping a post renumbers the rest. A held thread's unsent rest (`2/3`, `3/3`) keeps its marks in its text, since it carries on from the posted `1/3`.

In `-e`, a post can close with `image:` lines attaching images to that post, by any path `-i` takes (`~/…` and `pc:…` both work), each optionally followed by an `alt:` line:

```
the mesh, first pass
image: ~/renders/mesh.png
alt: a low-poly boat in wireframe
---
textured
image: pc:Pictures/Screenshots/boat.png
```

Each post keeps to the usual limits (up to 4 images, or one GIF or video), and a post can be just images. `-i` and `-s` images come first in the first post, with `--alt` for them as before, and a poll's post can't have `image:` lines. Only lines at the end of a post count, so a line of text that happens to start with "image:" stays text. The lines aren't counted, spell-checked or priced as links. Every image is read before any question and uploaded before the first post goes out; a thread that stops partway keeps the unsent posts' `image:` lines in the draft, so their images upload again (and alt text bills again) when `x-post -e` carries on. `--check` lists each post's images. Under `--at`, each post's images are copied into the queue with it.

To take back a typo, `x-post --undo` shows the last post x-post made and asks before deleting it (deletes cost $0.010):

```
x-post --undo
last post: 'havign a great day'
delete https://x.com/i/status/1850000000000000001? costs $0.010 [y/N] y
deleted https://x.com/i/status/1850000000000000001
```

After a thread, it takes back the whole thread, last post first, at $0.010 a post:

```
x-post --undo
last post: a thread of 3, starting 'first post'
delete all 3, from https://x.com/i/status/1850000000000000001? costs $0.030 [y/N]
```

A thread carried on from the draft (the `reply-to:` line) or from the queue (`--retry`) counts as one, as long as nothing else was posted in between. It goes back one post or thread only: the record (`~/.local/state/x-post/last.json`) is cleared once it's deleted. If X refuses a delete, whatever is still up stays on record, so `x-post --undo` again carries on.

For the link to something you posted, without opening X, `x-post --history` lists the last 10 posts x-post made, newest first (`--history 30` for more), marking any since deleted. It reads x-post's own log (the one `--spent` adds up), so it needs no credentials and covers what x-post has posted since that log began:

```
x-post --history
Fri 2026-10-23 18:20  https://x.com/i/status/1850000000000000003  'devlog: the boat gets a sail' and 2 more posts
Fri 2026-10-23 14:05  https://x.com/i/status/1850000000000000002  'tea or coffee?'
Fri 2026-10-23 13:50  https://x.com/i/status/1850000000000000001  'havign a great day' (deleted)
```

A thread is one line under its first post's URL, counted as one toward the 10, and marked `(1 of 3 deleted)` or `(deleted)` as its posts come down; one carried on from the draft or the queue stays one line. Threads posted before x-post began logging what each post replies to (late on 2026-10-23) show a line per post.

To take down an older post, `x-post --delete POST` takes its URL (any of X's forms, `x.com/i/status/…` or `x.com/<you>/status/…?s=20`) or bare id, shows its text and asks first ($0.010):

```
x-post --delete https://x.com/i/status/1850000000000000002
post: 'tea or coffee?'
delete https://x.com/i/status/1850000000000000002? costs $0.010 [y/N] y
deleted https://x.com/i/status/1850000000000000002
```

A post from before the log began can be deleted too, though without its text to show. One already deleted is refused, without the charge. A deleted post leaves the `--undo` record, so `--undo` never tries it again. In a thread it takes down just that post; the replies after it stay up.

## Scheduling

`--at` queues a post to go out later instead of now:

```
x-post --at 'tomorrow 09:00' 'good morning'
scheduled 3f2a for Sat 2026-10-24 09:00
```

Times are local: `+90m`, `+2h`, `+1d`, `09:00` (the next one, so tomorrow's once today's has passed), `tomorrow 09:00`, `fri 18:00` (the next one, a week out if it's Friday after 18:00), or `2026-10-24 18:30`. A time already past is refused. The offsets count on the wall clock, so `+2h` across a daylight-saving change is an hour off.

`--at` goes with everything else: `-e` threads and their `image:` lines, `-i` and `-s` images, `--alt`, `--poll`. Every check and question happens when you schedule (length, image vetting, typos, the link price, the [budget](#cost)), so sending later never asks anything, and the images are copied into the queue, so moving or deleting the originals afterwards is fine. `--check --at` also says when it would go out.

`x-post --scheduled` lists the queue, soonest first, with what each post will bill when it goes out (its posts, link posts and alt texts at X's prices), and `x-post --cancel ID` takes one out. Neither needs credentials:

```
x-post --scheduled
b7e2  Fri 2026-10-23 08:00  $0.015  'good morning'
      held: 403 Forbidden: Duplicate content.
3f2a  Fri 2026-10-23 09:30  $0.020  'with a picture', 1 image
9c01  Fri 2026-10-23 18:00  $0.215  'a thread' and 2 more posts
x-post --cancel b7e2
cancelled b7e2: 'good morning'
```

`x-post --move ID WHEN` changes when a queued post goes out, with WHEN read as for `--at` (`x-post --move 3f2a 'tomorrow 09:00'`). A held post stays held, so `--move` then `--retry` sends it at the new time. With a [budget](#cost) set, a move into another month is checked against that month as `--at` would check it (off a terminal it's refused: `…, past your $1.00 budget; not moved`); a move within the month isn't, since its total stays the same.

`x-post --revise ID` opens a queued post in your editor, laid out as in `-e` (`---` between a thread's posts, and a `reply-to:` first line on a held thread waiting to carry on). Each post's images show as `image:` lines (with their `alt:` lines) naming their copies in the queue, so you can move, add or drop them as in `-e`. Saving runs the same image checks, length check, typo question, link-price question and budget check as `--at`; any poll stays as it was. The post is locked while the editor is open, so the timer can't send it mid-edit; one that comes due meanwhile goes out at the next run after you save. An edit that fails a check (or a link price you decline) stays in the post's `revise.txt`, and the next `--revise` reopens it; saving it empty drops the edit and leaves the post alone.

`x-post --send-due` sends every queued post whose time has come, however late. If X refuses one, it's held with X's reason instead of being retried every minute; a thread that stops partway is held as the unsent rest, replying to the last post sent. When the cause has passed (X was down, the network dropped), `x-post --retry ID` releases it to the timer's next run, within a minute; a thread carries on where it stopped. Like `--cancel`, it needs no credentials:

```
x-post --retry b7e2
b7e2 goes out within a minute: 'good morning'
```

Since holds happen while you're away, every other x-post run (posting, `-e`, `--check`, `--at`, `--undo`, `--delete`) starts with a reminder until you retry or cancel:

```
x-post: 1 scheduled post is held; x-post --scheduled shows why
```

The moment the timer holds a post, it can also raise an alert: set `alert` in `~/.config/x-post/config.toml` to a command, and x-post runs it with the title and body after its own words (`~` at its start is expanded):

```toml
alert = "notify-send --urgency=critical"
```

A script of your own there can route it however you like (a desktop toast at the desk, a phone push away). Without the setting there's no alert, only the journal line and the reminder above. If the alert can't go out, the journal says so and the post is held all the same. The queue lives in `~/.local/state/x-post/scheduled/`, one folder per post. Each post has a lock: while the timer is sending one, `--cancel` and `--retry` say to try again in a moment, and the timer leaves a post someone is changing for its next run.

You never run `--send-due` yourself: the first `--at` installs a systemd user timer, `x-post-due.timer` (in `~/.config/systemd/user/`), that runs it every minute, so a post goes out within a minute of its time. Each later `--at` makes sure the timer is still enabled and running, and updates the units if a newer x-post changed them. While nothing is queued, the service is skipped without starting x-post. The journal gets only x-post's own lines (each post's URL, any failure), not systemd's per-minute "Starting", "Finished" or "skipped" lines. If the machine was off at the time, the post goes out within a minute of boot. Posts only go out while you're logged in unless lingering is on (`loginctl enable-linger`). If the timer can't be started (say, no user session bus), the post isn't queued. What it sent and any failures are in `journalctl --user -u x-post-due`.

## Cost

X has no free posting tier for new developers (closed February 2026). The pay-per-use API charges about **$0.015 per post**, or **$0.20 if the post contains a URL**. Credits are prepaid in the Developer Console; check current rates there before relying on these numbers.

Because of that gap, x-post asks before sending anything link-like (`https://…`, `www.…`, or a bare domain such as `example.com`), and only `y` or `yes` sends it. `e` opens the post in your editor to take the link out (or rephrase it), then checks it again:

```
cmon1975.com: link posts cost $0.20, not $0.015. post anyway? e edits [y/N/e]
```

X doesn't document what it counts as a link, so the check errs toward asking: `node.js` or `file.py` will ask too.

`x-post --spent` adds up what x-post has spent, from its own log of every billed call (`~/.local/state/x-post/log.jsonl`): posts, link posts, alt texts and deletes, each at the price listed when it was made. It needs no credentials and never contacts X:

```
x-post --spent
October 2026: $0.245 (2 posts, 1 link post, 1 alt text, 1 delete)
since 2026-10-23: $0.245 (2 posts, 1 link post, 1 alt text, 1 delete)
queued for November 2026: $0.035 (2 posts, 1 alt text)
```

The `queued for` lines, one per month with posts waiting in the `--at` queue, are what those posts will bill when they go out. An overdue one (held, or the machine was off) goes under the current month, since it goes out at the next run.

Being x-post's own count, it misses anything posted elsewhere or before the record began, counts as a link post whatever the link check flags (`node.js` included), and takes uploads and refused posts as free, which is what's been seen so far. The Developer Console has the real balance.

To cap a month's spending, put a budget in dollars in `~/.config/x-post/config.toml`:

```toml
budget = 1.00
```

Then, once every other question is answered, x-post adds what the post would cost (each post at $0.015 or $0.20, plus $0.005 per alt text) to what that month has already spent (from the same log) and what's queued to go out in it, and if the total passes the budget, asks first:

```
this would bring October 2026 to $1.040 ($0.045 of it queued), past your $1.00 budget. post anyway? [y/N]
```

Off a terminal it refuses instead. The queue counts because a post scheduled with `--at` is checked when you schedule it, against the month it goes out, and never again when the timer sends it. A queued post that's overdue (held, or the machine was off) counts toward the current month, since it goes out at the next run. `--revise` checks a queued post again, counting the revision in place of what it replaces, and `--move` checks one moved into another month. Deletes count toward the month's spend but aren't checked. Without the file (or without a `budget` line), nothing is. With one, `--spent` shows the month against it: `October 2026: $0.080 (4 posts, 2 alt texts, 1 delete) of your $1.00 budget` (or `, past your` once a `y` has taken it over), and each `queued for` line where its month would come to with the queue, counted as the checks count it: `queued for November 2026: $0.035 (2 posts, 1 alt text), making $0.035 of your $1.00 budget`.

## Setup

x-post needs Python 3.14 (uv fetches it if need be), an X developer account with pay-per-use credits, and, for `--at`, Linux with a systemd user session. Two features are off until `~/.config/x-post/config.toml` sets them up: `-s` needs a Windows machine reached over ssh (see the `-s` passage above), and the alert for a held post a command to run (see [Scheduling](#scheduling)).

1. Sign in to the X Developer Console (console.x.com) with the account you want to post as, and create an App.
2. In the App's **User authentication settings** (click **Set up**), set App permissions to **Read and write**. If it insists on a callback or website URL, any placeholder (`http://127.0.0.1/`) works; x-post never uses it. Any OAuth 2.0 Client ID and Secret it shows afterwards aren't needed.
3. Under **OAuth 1.0 Keys**, click **Regenerate** on **Consumer Key** (older docs call it the API Key) and copy both the key and the secret; they're shown only once. Then **Regenerate** the **Access Token** and copy it and its secret. Do this *after* step 2: a token made while the App was read-only stays read-only (the console labels it "Read"), and posting fails with a 403 about "oauth1 app permissions". The regenerated one should say "Read and write".
4. Buy some credits.
5. Put the four values in `~/.config/x-post/credentials.toml` (mode 600):

   ```toml
   api_key = "..."              # Consumer Key
   api_secret = "..."           # Consumer Secret
   access_token = "..."
   access_token_secret = "..."
   ```

6. Install from a clone, which puts `x-post` in `~/.local/bin` (add `--editable` to have code changes take effect without reinstalling):

   ```
   git clone https://github.com/CMON1975/x-post
   cd x-post
   uv tool install .
   ```

## Length

x-post refuses, before sending, any post that X would fold behind "Show more":

- **Over 280 characters as X counts them.** That's not `len()`: a link counts 23 however long it is, an emoji 2 (even a family or a flag made of several code points), CJK characters 2 each. Anything link-like counts as a link here, so `node.js` costs 23; the error is always toward refusing.
- **Over 10 lines.** This one comes from a 2025 browser test, not X's docs, so it's the less certain limit. Long lines also wrap into extra lines on narrow screens, which no count can predict.

```
x-post: 294/280 characters; X folds longer posts behind "Show more"
```

To measure a post without sending it, add `--check`. It needs no credentials, never contacts X, and exits 1 if the post wouldn't fit. With `-e` it checks the draft and keeps it, so you can trim, re-check, then post with `x-post -e`:

```
x-post --check "i'm having a great day 🎉 see cmon1975.com"
53/280 characters, 1/10 lines: fits
links: cmon1975.com, so it would cost $0.20, not $0.015
cost: $0.200 (1 link post)
```

The `cost:` line adds up the posts (a thread's too) and alt texts at X's prices. With a [budget](#cost) set, a line after it says where the month would come to, `October 2026 would come to $0.115 of your $1.00 budget` (or `, past your`), counting the month an `--at` post goes out and what's queued for it, as the check does.

At a terminal, a post past either limit gets a question instead of a flat refusal, and `y` opens it in your editor to trim, then checks it again:

```
294/280 characters; X folds longer posts behind "Show more". edit it now? s splits it into a thread [y/N/s]
```

`s` splits it into a thread for you and opens that in the editor, `---` lines and all, so you see where the breaks fall before saving posts it ($0.015 a post). Each post is filled as far as it goes, breaking after a sentence's end or at a line break, and between words only where one sentence is too long on its own (a single word over 280 characters can't be split, and says so). In a thread, only the posts too long are split; a split post's `image:` lines stay with its first piece, and a `reply-to:` line stays put. `--revise` offers the same.

Otherwise (no, or not at a terminal) the refused post becomes the draft, so `x-post -e` opens it to trim, whether it came from the command line, the prompt or `-e` itself. The one exception: it never overwrites an unsent draft, and says so instead (and for the same reason `e` and `y` above won't move a post into the editor over one).

## Typos

Before a post goes out (or is scheduled, or revised), x-post looks its words up in a US English dictionary (pyspellchecker's 160,000 words) and asks about any it doesn't know:

```
possible typos: havign (having?). post anyway? a adds them to your words, e edits [y/N/a/e]
```

`y` posts as is. `a` adds the words to `~/.config/x-post/words.txt` and posts, so a real word the dictionary lacks (`nvim`, `tailnet`, `devlog`) only comes up once; the file is one word per line and yours to edit. `e` opens the post in your editor there and then: it moves into the draft, and once you save, everything is checked again and it goes out (or, under `--at`, is queued) as if you'd used `x-post -e`. Anything else keeps the post back as the draft, so `x-post -e` opens it to fix. A thread is asked about once, for all its posts. Links, `#tags`, `@mentions`, anything with a digit (`pc-2`) and words with capitals past the first letter (`EEVEE`, `OptiX`) aren't looked up. `--check` lists possible typos without asking. The check costs nothing but time: about 0.3 s to load the dictionary, a little more when there's a correction to find.

## Missing dashes

Since unquoted words are posted as they stand, `x-post cancel 65a0` (meant as `x-post --cancel 65a0`) would post "cancel 65a0" for everyone to see. So a post given on the command line that reads as one of x-post's commands, its name followed only by what that command takes (`undo`, `spent`, `history 5`, `cancel 65a0`, `move 65a0 fri 18:00`, `delete <post>`), is asked about before anything else, even reading your credentials or fetching `-i` and `-s` images (`--check` just measures it):

```
post 'cancel 65a0'? it looks like x-post --cancel 65a0 without its dashes; r runs that [y/N/r]
```

`r` runs the command instead, just as if you'd typed the dashes, so `undo` and `delete` still show the post and its price and ask before deleting; `move`'s time may run over several words (`move 65a0 fri 18:00` runs `x-post --move 65a0 'fri 18:00'`). `y` posts it as text. Anything else does neither, and the text isn't kept as the draft. A command missing what it takes (`cancel` alone) has no `r`, only `[y/N]`. A post that only starts with a command's name (`check this out`, `cancel the meeting`) isn't asked about, nor is one typed at the prompt or written in the editor, where the slip can't happen.

## Tab completion

`completions/x-post.bash` completes the options, the queued post ids after `--cancel`, `--retry`, `--move` and `--revise`, your logged post ids after `--delete` (newest first, deleted ones left out), and file names after `-i`. It needs only bash's own `complete`, not the bash-completion package, so `~/.bashrc` sources it directly from wherever you cloned x-post:

```bash
[[ -r ~/x-post/completions/x-post.bash ]] && . ~/x-post/completions/x-post.bash
```

A test holds its option list to the parser's, so a new flag that isn't added there fails the suite.

## Quoting

The prompt avoids all of this: bash never sees what you type there. On the command line, prefer single quotes. An unquoted `#` starts a comment, so `x-post loving it #arch` posts just "loving it". Inside double quotes, `!` triggers history expansion (`"wow!!"` repeats your last command). Unquoted words also work and are joined with spaces (`x-post having a great day`), but an apostrophe then needs quoting.

## Development

`uv run pytest`. The tests intercept all HTTP, so they never reach X, and run in scratch config and state folders, so they never touch your credentials, draft or queue.

The App's icon in the Developer Console is `assets/icon.png`: Lucide's `send` (ISC) in white on slate, full bleed, with room for a circular crop. `assets/icon.svg` is the source, and a test holds its paths to those in the `icons/` folder of a Lucide clone that `LUCIDE_ICONS` names (skipped without it). After changing it: `rsvg-convert -w 1024 -h 1024 assets/icon.svg -o assets/icon.png`.

## License

MIT, in `LICENSE`. The icon in `assets/` is Lucide's `send`, under Lucide's ISC license, kept in `assets/LICENSE-lucide`.

Written by CMON1975 with Claude (Anthropic), through Claude Code.
