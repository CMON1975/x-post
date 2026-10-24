# Undo, history and delete

These work on the posts x-post has made, from its own records, so you never have to look them up on X.

## Undo

`x-post --undo` shows the last post x-post made and asks before deleting it ($0.010):

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

- A thread carried on from the draft (its `reply-to:` line) or from the queue (`--retry`) counts as one thread, as long as nothing else was posted in between.
- It goes back one post or thread only. The record, `~/.local/state/x-post/last.json`, is cleared once it's deleted.
- If X refuses a delete, whatever is still up stays on record, so running `x-post --undo` again carries on.

## History

`x-post --history` lists the last 10 posts or threads x-post made, newest first, with their URLs. `--history 30` shows more:

```
x-post --history
Fri 2026-10-23 18:20  https://x.com/i/status/1850000000000000003  'devlog: the boat gets a sail' and 2 more posts
Fri 2026-10-23 14:05  https://x.com/i/status/1850000000000000002  'tea or coffee?'
Fri 2026-10-23 13:50  https://x.com/i/status/1850000000000000001  'havign a great day' (deleted)
```

- A thread is one line, under its first post's URL, marked `(1 of 3 deleted)` or `(deleted)` as its posts come down.
- It reads x-post's own log, the one `--spent` adds up, so it needs no credentials and covers only what x-post has posted.

## Delete

`x-post --delete POST` takes down an older post. POST is its URL, in any of X's forms (`x.com/i/status/…` or `x.com/<you>/status/…?s=20`), or its bare id from `--history`. It shows the post's text and asks first ($0.010):

```
x-post --delete https://x.com/i/status/1850000000000000002
post: 'tea or coffee?'
delete https://x.com/i/status/1850000000000000002? costs $0.010 [y/N] y
deleted https://x.com/i/status/1850000000000000002
```

- A post from before x-post's log began can be deleted too, though without its text to show.
- A post already deleted is refused, without the charge.
- In a thread, it takes down just that post. The replies after it stay up.
- A deleted post leaves the `--undo` record, so `--undo` never tries it again.
