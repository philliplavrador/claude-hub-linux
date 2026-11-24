# Auto-switch: how a Claude moves to another subscription

`claude-autoswap` watches every Claude subscription you have set up (one folder per subscription under
`~/.claude-accounts/`) and moves a running Claude to another subscription just before it hits a usage limit. The
decision is arithmetic on numbers it reads. No model is asked anything, ever.

This page describes what it does and the rule it uses. `claude-autoswap status` shows its current readings and
plans.

## What it watches

For each subscription, every 60 seconds:

- the **5-hour** meter, the **week** meter, and the **per-model week** meter for Fable, each as a percentage with a
  reset time;
- how many of that subscription's Claudes are **busy** (working on a reply right now);
- how fast the 5-hour meter has been rising: the slope of the last 10 minutes of readings, never below zero.

A meter whose reset time has already passed counts as 0.

Where the numbers come from:

- Claude Code's status line reports the 5-hour and week meters of the session on screen. The watcher keeps those
  readings, so they are fresh whenever a Claude of that subscription is open.
- The usage endpoint that Claude Code itself reads (`/api/oauth/usage`, called with the subscription's own access
  token) is asked at most every 5 minutes per subscription, mainly for the per-model figure. It is not documented by
  Anthropic and could change; if it does, the status-line readings still work. For each meter the newer reading wins.
  Set `CLAUDE_HUB_AUTOSWAP_USAGE_URL` to point it at a different server (the tests use a fake one).
- A subscription whose sign-in has lapsed is not asked at all. It is not a target until a Claude has run there again.

The watcher only reads. It never refreshes or writes a credential, never starts a Claude that was not already
running, and never stops one it will not restart.

## When a Claude must leave

A Claude on subscription *a*, running model *m*, must leave when any of these is true:

1. the week meter is at 100 percent or more;
2. *m* is Fable and the Fable week meter is at 100 percent or more;
3. the 5-hour meter would run out within 10 minutes at its current speed (the meter plus the speed times 10
   minutes reaches 100). The speed is the sum of every working Claude's burn on that subscription, so two working
   Claudes trip this about twice as early as one;
4. the 5-hour meter is at 98 percent or more (an idle Claude would hit the wall on your next message);
5. it hit a limit: a `StopFailure` hook flag (installed in each subscription's settings), a blocked Claude whose last
   line is a rate-limit error, or Claude Code's own "usage limit reached, a short grace allowance remains" note.

## When it may move

Rules 1 to 4 move a Claude only when it is **idle**: not busy, not waiting for your answer, with no unsent text in its
prompt, not the window that has your keyboard focus, and not within 30 minutes of its last move.

Rule 5 (a real hit) ignores the 30 minutes and the focus, because the Claude has stopped anyway, but it still never
types over unsent text. A Claude waiting for your answer on a spent subscription is moved too, and told that it moved
so it asks the same question again on the new subscription. It is never moved off the desktop you are looking at or
over an answer you have started.

## Where it goes

Take each other subscription *b*, and let *m* be the model the Claude will run there. *b* is **eligible** when all of
these hold:

- its week meter is under 95 percent (the line of 100 minus a margin of 5);
- if *m* is Fable, its Fable meter is under 95 percent;
- its 5-hour meter plus speed times 10 minutes is under 90;
- its latest reading is not a failed fetch.

Each eligible subscription also gets a score:

```
U = room / hours to reset            use it before you lose it
    room  = 100 - Fable%  for a Fable Claude,   100 - week%  otherwise
    hours = max(0.5, hours until that meter resets)

B = 1 + (week% - Fable%) / 100       for a Fable Claude
    1 + (Fable% - week%) / 100       otherwise

C = (100 - 5-hour% - speed x 10) / 100       5-hour room for the next ten minutes

score = U x B x C / (1 + busy Claudes on b)
```

In words: prefer the subscription whose spare capacity will be lost soonest (U); send a Fable Claude where Fable is
lagging behind the week, and an Opus Claude where Fable is already spent (B); keep room for the next 10 minutes (C);
and spread the work, so fewer busy Claudes wins.

The score is not the first thing that decides, though. The eligible subscriptions are ordered by:

1. a week that ends within 24 hours and still has 10 or more points of room under the 95 target, first, because
   that room is lost at the reset;
2. then the week that resets soonest;
3. then the highest score, which settles subscriptions that reset together;
4. then the fixed account order.

After each move the target counts one more busy Claude before the next Claude is placed in the same cycle.

**Fable used up everywhere.** A Fable Claude that must leave and finds no eligible subscription for Fable is placed
again as an Opus Claude, using the same formula, and switches model on the way: `opus[1m]` when its conversation is
over 150,000 tokens, `opus` otherwise. If its own subscription is the best Opus choice, it stays and only switches
model.

**Nowhere to go.** With no eligible subscription the Claude stays where it is. The hub row shows when it can move
again: the earliest reset time that would make a subscription eligible.

**New Claudes.** The recommended subscription for a new Claude is the first in the same order, for an Opus Claude,
with the 3-point room for the first rule. The account menu marks it; you still pick.

**Pulls.** A Claude that is nowhere near a limit is also moved, under the same idle rules, onto a subscription whose
week ends in 1 to 24 hours, that has 10 or more points of room under the 95 target and a 5-hour meter that would stay
under 75, so that room is used before it is lost. Three pulls per cycle at most, and never off a week that ends
within 48 hours while it still has room.

## Moving one Claude (`claude-move`)

`claude-move <session id> <account> [--model M] [--continue]` moves a single Claude:

1. It finds the Claude from `claude agents --json` of its subscription and the process tree (a plain Claude's Konsole
   tab, or a background Claude's window record).
2. It refuses (exit 3, nothing touched) if the Claude is busy, waiting for you, or has unsent text in its prompt.
3. It copies the conversation transcript into the target subscription, under the same project folder.
4. It stops the Claude and resumes the same conversation with `--resume` under the target subscription, passing
   `--model` when asked. A plain Claude is relaunched in the same Konsole tab.
5. With `--continue` the resumed Claude is sent `continue`, so a reply cut off by a limit carries on.
6. The move is logged with its reason, the rule number and the scores.

A move can change the session id when the target subscription has never run that session. Everything keyed by the id
follows the new one the move reports.

Moving has a cost: subscriptions share no prompt cache, so the first reply after a move re-reads the whole
conversation. That is why it only moves close to a limit, never to balance load for its own sake.

## Which Claudes it touches

Only the Claudes the hub lists: background sessions and plain Claudes in a Konsole on your screen. Never the
claude-mem observer sessions, never a Claude on the hidden agent screen, and never another tool's workers.

## Failures

- A usage fetch that fails keeps the previous reading and its age. A subscription whose only reading is a failure is
  never a target. With no readings at all the watcher makes no moves, and only rule 5 (a real hit) acts.
- A `429` from the usage endpoint backs that subscription off to 5 minutes.
- A move that fails or is refused leaves the Claude where it was, logs why, and is not tried again for 10 minutes,
  so a Claude you are typing in never gets keystrokes injected every minute. One exception: a refused Claude that then
  hits a real limit on a subscription it must leave is tried once more at once.

## Constants

The constants sit at the top of the script: week line 100, Fable line 100, 5-hour line 98, target margin 5 (a
destination needs both week meters under 95), 5-hour target 90, horizon 10 minutes, minimum gap between moves 30
minutes, retry gap 10 minutes.

## Not done

Balancing Claudes in general (the pulls above are the one exception), refreshing an idle subscription's token, notifications on each
move, and any API-key fallback.
