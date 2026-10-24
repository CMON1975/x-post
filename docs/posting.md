# Posting

## From the command line

```
x-post 'having a great day'
https://x.com/i/status/1850000000000000001
```

x-post prints the new post's URL and exits 0. On failure it prints one line to stderr, with X's own reason where there is one (duplicate content, bad credentials), and exits 1.

Unquoted words are joined with spaces, so `x-post having a great day` works too. See [Quoting](#quoting) for the characters the shell gets to first.

## At the prompt

Run `x-post` with no message and type the post at the `post:` prompt. The shell never sees that line, so nothing needs quoting:

```
x-post
post: it's #great! costs $5
https://x.com/i/status/1850000000000000001
```

Ctrl-C or Ctrl-D cancels without posting.

## In your editor

`x-post -e` opens your editor, for a post with line breaks or a [thread](#threads). It uses `$VISUAL`, then `$EDITOR`, then the first of nvim, vim, vi and nano that's installed.

- Save and quit to post.
- Quit with the file empty, or with `:cq`, to cancel.

The draft lives at `~/.local/state/x-post/draft.txt` until it's posted. A post X rejects, or one you decline at a question, stays there, and the next `x-post -e` reopens it.

## Threads

In the editor, a line of just `---` separates the posts of a thread:

```
first post
---
second post
---
third post
```

- Every post is checked before any is sent, and the [link question](checks.md#links) is asked once for the whole thread.
- The posts go out as a reply chain ($0.015 each), and each URL prints as it lands.
- `---` only splits in the editor. On the command line or at the prompt it's just text.

If a post fails partway, the unsent posts stay in the draft under a first line `reply-to: https://x.com/i/status/<last one sent>`, and `x-post -e` carries on from there. Delete that line to start a fresh thread instead.

X only lets the API reply to your own posts (or to posts that mention you), which is all a thread needs.

### Numbering

`x-post -e --numbered` ends each post with its place: `first post 1/3`, `second post 2/3`, `third post 3/3`.

- The marks are added as the posts go out, not written into your draft, but they count toward each post's length. `--check --numbered` measures them.
- A post of just images gets the mark alone. A single post gets none.
- A numbered thread that stops partway keeps its marks in the draft, and carrying it on, with `--numbered` or without, leaves them as they are. A post that merely ends in a fraction (`50/50`) is still marked.
- For a [scheduled](scheduling.md) thread, `--revise` opens it without the marks and numbers it again when you save, so adding or dropping a post renumbers the rest. A held thread's unsent rest keeps its marks, since it carries on from the posts already out.

## Polls

```
x-post --poll tea --poll coffee --poll-for 6h 'tea or coffee?'
```

| Part | Rule |
| --- | --- |
| Options | `--poll` once for each, 2 to 4, up to 25 characters each |
| Question | the post's text, so it can't be empty |
| Open for | `--poll-for`: `30m`, `6h`, `2d`, anything from 5 minutes to 7 days. A day if not given |

- The options and the time are checked before anything is sent, and `--check` shows them: `poll for 6h: 'tea', 'coffee'`.
- A poll can't go with images or video, since X takes one or the other.
- In a thread it goes with the first post. With `--at` it's queued with the post, and its clock starts when the post goes out.
- It bills as a plain post ($0.015): X's price list has no line for polls.

## Quoting

The prompt and the editor avoid all of this. On the command line:

- Prefer single quotes.
- An unquoted `#` starts a comment, so `x-post loving it #arch` posts just "loving it".
- Inside double quotes, `!` triggers history expansion: `"wow!!"` repeats your last command.
- Unquoted words work, but an apostrophe then needs quoting.

A post that reads like an x-post command typed without its dashes (`x-post cancel 65a0`) is asked about before it goes out. See [Missing dashes](checks.md#missing-dashes).
