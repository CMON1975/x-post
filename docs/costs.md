# Costs

X's API has had no free posting tier for new developers since February 2026. It's pay-per-use, from credits you prepay in the Developer Console (console.x.com). These are the prices x-post works with; check the current rates in the console before relying on them.

| Call | Price |
| --- | --- |
| Post | $0.015 |
| Post with a link | $0.20 |
| Alt text | $0.005 each |
| Delete | $0.010 |
| Uploads, `--check`, posts X refuses | free, as far as seen so far |

Image posts and polls bill as plain posts. Because a link costs so much more, x-post [asks before a link post](checks.md#links).

## What you've spent

`x-post --spent` adds up x-post's own log of every billed call (`~/.local/state/x-post/log.jsonl`), each at the price listed when it was made. It needs no credentials and never contacts X:

```
x-post --spent
October 2026: $0.245 (2 posts, 1 link post, 1 alt text, 1 delete)
since 2026-10-23: $0.245 (2 posts, 1 link post, 1 alt text, 1 delete)
queued for November 2026: $0.035 (2 posts, 1 alt text)
```

- Each `queued for` line, one for each month with posts waiting in the [queue](scheduling.md), is what those posts will bill when they go out. An overdue post goes under the current month, since it goes out at the next run.
- Since it's x-post's own count, it misses anything posted elsewhere or before its log began, and counts whatever the link check flags as a link post. The Developer Console has the real balance.

## Monthly budget

To cap a month's spending, set a budget in dollars in `~/.config/x-post/config.toml`:

```toml
budget = 1.00
```

Once every other question is answered, x-post adds the post's cost to what the month has spent and what's queued for it. If the total passes the budget, it asks first:

```
this would bring October 2026 to $1.040 ($0.045 of it queued), past your $1.00 budget. post anyway? [y/N]
```

Off a terminal it refuses instead.

- A post scheduled with `--at` is checked against the month it goes out in, when you schedule it, and never again when it's sent. That's why the queue counts.
- `--revise` checks again, counting the new version in place of the old one. `--move` checks a post moved into another month.
- Deletes count toward the month's spend but aren't checked.
- `--check` adds a line saying where the month would come to.
- `--spent` shows each month against the budget, `October 2026: $0.080 (4 posts, 2 alt texts, 1 delete) of your $1.00 budget` (or `past your`), and each `queued for` line with where its month would come to.

Without a `budget` line, nothing is checked.
