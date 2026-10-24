# Checks before sending

Before anything goes out, x-post checks the post and asks about anything that looks wrong. Length, typos and links are checked again when you schedule a post with `--at` or rewrite one with `--revise`.

## Length

x-post refuses any post that X would fold behind "Show more":

- **Over 280 characters, as X counts them.** A link counts 23 however long it is. An emoji counts 2, even one made of several code points (a family, a flag), and so does each CJK character. Anything link-like counts as a link, so `node.js` counts 23.
- **Over 10 lines.** This limit comes from a 2025 browser test, not X's docs, so it's the less certain one. Long lines can also wrap into extra lines on narrow screens, which no count can predict.

```
x-post: 294/280 characters; X folds longer posts behind "Show more"
```

### Measuring without sending

`--check` measures a post without credentials or contacting X, and exits 1 if it wouldn't fit. With `-e` it checks the draft and keeps it, so you can trim, check again, then post with `x-post -e`:

```
x-post --check "i'm having a great day 🎉 see cmon1975.com"
53/280 characters, 1/10 lines: fits
links: cmon1975.com, so it would cost $0.20, not $0.015
cost: $0.200 (1 link post)
```

It also lists images, the poll and possible typos. The `cost:` line adds up the posts and alt texts at X's prices. With a [budget](costs.md#monthly-budget) set, a last line says where the month would come to: `October 2026 would come to $0.115 of your $1.00 budget`.

### Fixing a post that's too long

At a terminal, a post past either limit gets a question instead of a flat refusal:

```
294/280 characters; X folds longer posts behind "Show more". edit it now? s splits it into a thread [y/N/s]
```

| Answer | Does |
| --- | --- |
| `y` | opens it in your editor to trim, then checks it again |
| `s` | splits it into a thread and opens that in your editor, so you see where the breaks fall before saving posts it ($0.015 a post) |
| anything else | keeps it as the draft, so `x-post -e` opens it to trim |

Off a terminal, the post becomes the draft without asking. x-post never overwrites an unsent draft, though: if there is one, it says so instead.

When `s` splits a post:

- Each post is filled as far as it goes, breaking after a sentence's end or at a line break, and between words only where a sentence is too long on its own. A single word over 280 characters can't be split, and x-post says so.
- In a thread, only the posts that are too long are split. A split post's `image:` lines stay with its first piece, and a `reply-to:` line stays put.
- With `--numbered`, it leaves room for the marks, two digits' worth from ten posts on.
- `--revise` offers the same split.

## Typos

x-post looks each post's words up in a US English dictionary (pyspellchecker's 160,000 words) and asks about any it doesn't know:

```
possible typos: havign (having?). post anyway? a adds them to your words, e edits [y/N/a/e]
```

| Answer | Does |
| --- | --- |
| `y` | posts it as is |
| `a` | adds the words to `~/.config/x-post/words.txt`, then posts |
| `e` | opens the post in your editor; when you save, it's checked again and goes out (or is queued) as if you'd used `x-post -e` |
| anything else | keeps it as the draft, so `x-post -e` opens it to fix |

- `words.txt` has one word per line and is yours to edit, so a real word the dictionary lacks (`nvim`, `devlog`) comes up only once.
- Links, `#tags`, `@mentions`, anything with a digit, and words with capitals past the first letter (`EEVEE`, `OptiX`) aren't looked up.
- A thread is asked about once, for all its posts. `--check` lists possible typos without asking.
- Loading the dictionary adds about 0.3 seconds.

## Links

A post with a link bills $0.20 instead of $0.015, so x-post asks first. Anything link-like counts: `https://…`, `www.…`, or a bare domain such as `example.com`.

```
cmon1975.com: link posts cost $0.20, not $0.015. post anyway? e edits [y/N/e]
```

Only `y` or `yes` sends it. `e` opens the post in your editor to take the link out or rephrase it, then checks it again. X doesn't document what it counts as a link, so the check errs toward asking: `node.js` and `file.py` ask too.

## Missing dashes

Unquoted words are posted as they stand, so `x-post cancel 65a0`, meant as `x-post --cancel 65a0`, would post "cancel 65a0" for everyone to see. A post given on the command line that reads as one of x-post's commands, its name followed only by what that command takes (`undo`, `spent`, `history 5`, `cancel 65a0`, `move 65a0 fri 18:00`, `delete <post>`), is asked about before anything else, even before your credentials are read:

```
post 'cancel 65a0'? it looks like x-post --cancel 65a0 without its dashes; r runs that [y/N/r]
```

- `r` runs the command, as if you'd typed the dashes. Its own questions still apply, so `undo` and `delete` still show the post and its price and ask first. `move`'s time may run over several words (`move 65a0 fri 18:00`).
- `y` posts the text. Anything else does neither, and the text isn't kept as the draft.
- A command missing what it takes (`cancel` alone) gets only `[y/N]`.
- A post that only starts with a command's name (`check this out`) isn't asked about, nor is anything typed at the prompt or written in the editor. `--check` just measures it.
