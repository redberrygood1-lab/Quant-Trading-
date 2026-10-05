# The vague-term book

Every phrase a member writes on day 2 that cannot be measured, and the questions that turn it
into a number. Work down the "ask" column in order. Stop the moment the answer is a number and
a series from the allow-list in SCHEMA.md.

Rules for using this:
- **Never pick for them.** Offer the shapes an answer can take, then wait.
- **Never accept a range as a final answer.** "20 to 50 days" is two strategies. Pick one now,
  test the other later.
- **One vague term at a time.** Members give up when hit with a form.
- If the answer needs a series the allow-list does not have, try to express it another way
  before reaching for `custom`. `custom` blocks day 4.

---

## Strength and weakness

| They wrote | Ask |
|---|---|
| strong, strength, momentum | Above what? A moving average, a price N bars ago, or an oscillator level? Then: which period, what level |
| weak, weakness, falling apart | Same three shapes, other side |
| trending, in an uptrend | What makes it a trend to you: price above an average, a higher high than N bars ago, or ADX above a level? Then the number |
| overbought, oversold | Which indicator, what period, what level. If they say RSI, ask the period and the level separately |
| accelerating, picking up | Percent change over how many bars, above what percent |

## Levels and location

| They wrote | Ask |
|---|---|
| breaks out, breaks | Above what exact line: the highest high of the last N bars, a round number, a band? N is what |
| near, around, close to | Within what percent, or within how many ATRs |
| at support, at resistance | Support drawn how? The lowest low of the last N bars is the only version a computer can read. What is N |
| above the average | Which average, simple or exponential, over how many bars |
| holds, stays above | For how many consecutive bars |

## Pullbacks and timing

| They wrote | Ask |
|---|---|
| after a pullback, on a dip | Two numbers: how deep (percent off the high, or how many ATRs), and over what window |
| at some point recently | "At some point" is a window and a window needs an edge. How many bars back exactly. Turn it into an `offset` |
| soon after, shortly after | How many bars |
| a bit, a while, for a few days | The number of bars. 1, 2, 3, 5 |
| in the morning, at the open | Which bar exactly. On daily bars there is one bar a day, so this may mean the fill timing instead, see below |
| on Fridays, end of week | `day_of_week == 4`. Confirm which day, and confirm the market's week not theirs |

## Exits

| They wrote | Ask |
|---|---|
| looks tired, runs out of steam | Is that the opposite of your entry, a fixed number of bars, or a profit target? Pick, or pick more than one and say which fires first |
| take profits | At what: a percent, a multiple of the stop distance, a multiple of ATR |
| let it run | Then what ends the trade? A trailing stop needs a distance. A time stop needs a bar count |
| when I've had enough | How many bars is too long |
| get out before earnings | Not in the allow-list for week one. Offer a time stop instead, or log it as unresolved |

## Risk and size

| They wrote | Ask |
|---|---|
| don't size crazy, size sensibly | Two numbers. Where the stop sits, and what percent of the account is lost if it is hit |
| a small position | Percent of the account, or percent risked. Those are different, ask which they mean |
| I cut it quickly | Percent below entry, or a multiple of ATR. Which |
| I move my stop up | Trailing, then. Trailing by what distance |
| I don't use a stop | Allowed, `stop.type: "none"`. Say plainly: with no stop, risk sizing has nothing to measure, so sizing becomes a fixed fraction of the account, and one trade can take the account a long way down |
| a few trades at a time | `max_open_positions`, exact number. And total risk on at once |

## Instrument and data

| They wrote | Ask |
|---|---|
| stocks, crypto, forex | One symbol for week one. Which one. The exact ticker |
| the S&P | The index or an ETF you can actually trade? `^GSPC` or `SPY`. They behave differently |
| I trade a basket | One symbol for week one. Pick the one you know best. The others come later |
| intraday, short term | Which bar size. And say out loud that free intraday history is short, often under two years, which day 5 may not be able to work with |
| I've done this for years | Then start the history at the earliest date the data source has. Longer history, harder test |

## Words that hide a whole second rule

These almost always mean the member has two rules, not one. Say so, and make them pick one for
week one. The second one goes in a note for later.

- "unless the market is bad" (a regime filter)
- "if it's a good setup" (a discretionary override, which cannot be tested at all)
- "I use my judgement on the exit" (same)
- "depends on the news" (same)
- "and I add to winners" (a pyramiding rule, out of scope for v1)

For the three discretionary ones, be direct. Something like: whatever your judgement adds is not
in this test. If the rule needs your judgement to work, the backtest will measure the rule
without you, and that is worth knowing on its own.

---

## Answers that are not answers

Push back once on each of these, then log to `unresolved` if they hold.

- "whatever you think" → It has to be yours. A number you picked and can defend beats a better
  number I picked.
- "the standard one" → There isn't one. There are conventions. RSI 14 and ATR 14 are conventions.
  Say which convention you want and we use it.
- "it depends" → On what? If the thing it depends on is measurable, that is a second condition.
  If it isn't, it goes in unresolved.
- "roughly 20" → 20 or 21? Pick. You can test both after day 5, one at a time, knowing what
  changing it does to the overfitting score.
