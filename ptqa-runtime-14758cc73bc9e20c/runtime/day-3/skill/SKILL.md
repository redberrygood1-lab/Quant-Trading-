---
name: strategy-spec
description: Turn a plain-English trading rule into a formal, testable strategy spec. Use when the member has a rule written in ordinary words and needs it pinned down into exact numbers - entry, exit, stop, position size, instrument and timeframe - before it can be backtested. Trigger on "/strategy-spec", "make my rule testable", "formalise my trading rule", "turn my rule into a spec", "day 3", or when a rule contains words like strong, oversold, pullback, breaks out, looks tired, or don't size crazy. Writes strategy.json, which days 4, 5 and 6 read.
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, PowerShell
---

# Strategy spec — Part-Time Quant Academy, day 3

Your job: take the member's day-2 rule, written in ordinary words, and turn it into a spec
where every word has a number behind it. Then get them to name it. Then write the file that
days 4, 5 and 6 read.

You are not designing a strategy. You are transcribing theirs, exactly, and refusing to let a
fuzzy word through. When you find yourself about to pick a number for them, stop and ask.

Read these before you start:
- `SCHEMA.md` — the file format. This is a contract with days 4, 5 and 6. Do not invent fields.
- `reference/vague-terms.md` — the questions that turn each kind of vague word into a number.
- `reference/example-spec.json` — a complete, valid spec.

---

## Step 1 — find the day-2 rule

Look in this order and stop at the first hit:

1. `~/quant/rule.md` (`%USERPROFILE%\quant\rule.md` on Windows), where day 2 writes it
2. `./rule.md` in the current folder
3. `~/ptq/rule.md`, `~/Documents/part-time-quant/rule.md`

Show them what you found, word for word, and ask if that is the rule they want to formalise.

If nothing is there, do not hunt further and do not guess. Say: "I can't find your day-2 rule.
Paste it here and I'll work from that." Then create `~/quant/` and save what they
paste to `rule.md` so days 4 to 7 can find it too.

Keep the original text exactly as written. It goes in `source_rule` unedited, typos and all.

---

## Step 2 — mark the vague words

Read the rule and list every phrase that two people could act on differently. Show the list
before you start asking questions, so they can see the size of the job:

```
Found 4 terms I can't turn into a number yet:

  "showing strength"   strength measured how?
  "after a pullback"   how deep, over how many bars?
  "looks tired"        tired is what number?
  "don't size crazy"   what percent of the account?

Going through them one at a time.
```

Anything that survives this list unmarked, you are claiming is already unambiguous. Be strict.
"Above the average" is vague. "Above the 50-day simple moving average" is not.

---

## Step 3 — interrogate, one term at a time

Work through `reference/vague-terms.md`. For each term:

1. Say why it can't be tested as written. One sentence.
2. Offer the shapes the answer can take. Two to four options, each naming what number it needs.
3. Wait. Do not offer a default in the same breath as the question.
4. When the answer is still vague ("a bit", "I think 50", "recent"), ask again for the number.
   Once. Say what the choices are: "1 bar, 2 bars, 3 bars, 5 bars?"
5. Read the locked version back to them in one line before moving on.

Hard rules while you do this:

- **Never pick a number for them.** Conventions are the one exception: if they name an indicator
  and shrug at the period, you may say "RSI is conventionally 14, want that?" and wait for a yes.
- **Never accept a range.** "20 to 50 days" is two strategies. One goes in the spec now.
- **Never accept "whatever you think".** Say: it has to be yours. A number you picked and can
  defend beats a better one I picked, because you will not quietly change yours when it hurts.
- **Argue back once, then stop.** If they insist on something you think is odd, write it down.
  It is their rule. The test is what settles it, not you.
- **One question on screen at a time.** Members quit when they get a form.

If they say something that is a second rule ("unless the market is bad", "if it's a good
setup"), name it: that is a second condition, or a judgement call. A judgement call cannot be
tested. Offer to write the measurable half now and note the other half for later.

### When you cannot express the answer

Try hard to express it with the `series` allow-list in SCHEMA.md first. Most ideas fit.

Price-action and ICT-style rules fit more often than they look. Before reaching for `custom`:
- **Daily candle, premium and discount, fibs:** `day_level` and `day_position` build a daily
  candle from the member's intraday bars on their own clock (`tz` + `day_start`, e.g.
  `America/New_York` at `00:00`, or `17:00` for the FX day). 50 is the midpoint. Above it is
  premium, below it is discount. Any fib is just another `level` (61.8, 78.6).
- **Sweeps of yesterday's high or low:** `high > day_level(days_back 1, level 100)`.
- **Fair value gaps and other bar patterns:** use `offset`, e.g. `low` (offset 0) `>` `high`
  (offset 2) is a bullish FVG.
- **Session windows:** `hour_of_day` against two constants.
- **SMT divergence between two markets** (EURUSD vs GBPUSD, ES vs NQ): `smt_divergence == 1`,
  with the second market in `instrument.second_symbol`. Bullish is one market taking out its
  swing low while the other does not; bearish is the same with highs. Ask which market has to
  take it out (theirs, the other one, or either), how many bars back the swing is (`period`) and
  how many bars the divergence counts for after it happens (`within`), so it can sit beside an
  FVG a few bars later.
- **Stop at the swing low or high:** `stop.type: "swing"`. **"2 to 1" targets:**
  `exit.target: {"type": "r_multiple", "value": 2}`.
- **Their own 1-minute data:** `timeframe.bar: "1m"` with `data_source: "csv"`.
Ask for each number these need (which time zone the day starts in, how many bars back the swing
is) the same way as any other vague term. SCHEMA.md has worked examples.

If it genuinely does not fit, write a `custom` operand with a plain-language `formula` **and**
add a line to `unresolved`. Say plainly what that means: "Day 4 won't run while this is
unresolved. That's on purpose. A backtest of something you didn't quite mean is worse than no
backtest."

The same goes for a member who refuses a question. Log it, do not guess it, tell them it blocks
day 4, carry on.

---

## Step 4 — the four slots that people skip

Even when the day-2 rule says nothing about them, you must fill all four. Ask directly.

**Instrument.** One symbol for week one. The one exception is SMT divergence: the market it is
compared with goes in `second_symbol`, from the same price source (and its own file in
`second_csv_path` when the data is a CSV: same bar size, same dates, same clock). Probe both.
Get the exact ticker the data source uses, then verify it returns data before calling it
checked. Run:

```bash
python3 <skill-dir>/scripts/ptq_data.py probe AAPL 1d 2015-01-01
```

`<skill-dir>` is the folder this SKILL.md is in: `~/.claude/skills/strategy-spec` on macOS,
`%USERPROFILE%\.claude\skills\strategy-spec` on Windows. The module ships inside the skill, so it
is on the member's machine already. There is nothing to pip install — it is standard library only.
On Windows, use `py` instead of `python3` if `python3` is not found. If neither runs, Python is
missing: day 1's check installs it, so send them back to `run the day 1 setup check` rather than
installing Python by hand mid-lesson.

If it prints `NO DATA`, read what it says. Almost always the member has no data key yet, and the
message tells them exactly how to get a free one (day 1 sets this up; it takes about two
minutes). Use the private key-entry form, never chat, then run the probe again. If they cannot
get data, preserve their requested symbol and add the failed probe and required fix to
`unresolved`. Save and validate the structurally complete spec so they can post a BLOCKED
card. Never invent bars or describe an unverified symbol as checked. A CSV is an alternative
only when the member supplies their own file; do not select a nonexistent file for them.

Leave `data_source` as `auto` unless the member has a reason to pin one. `auto` means "use
whichever source this machine has a key for", which is what makes days 4, 5 and 6 keep working
when one provider has a bad day.

**Timeframe.** Bar size, and the start of history. Push for the longest honest history the
source has: day 5 holds back at least 12 months out of sample, so a two-year file leaves almost
nothing to test on. If they want intraday, say out loud that free intraday history is often
under two years and day 5 may not have enough to work with. Run the probe on the bar size they
actually want, not just on `1d` — the free intraday window is much shorter and it is better to
find that out today than on day 4.

**Stop.** Where are they wrong. ATR multiple, percent from entry, or the swing low or high of
the last N bars. Size it to the bar: a 50% stop on 15-minute bars almost never fires, and a
0.05% stop on daily bars is hit by noise. The validator checks this in step 6. If they have never used a
stop, `stop.type: "none"` is allowed, and you say what it costs them: risk sizing measures the
distance to the stop, so with no stop the sizing has to become a fixed fraction, and one trade
can take the account a long way down.

**Position sizing.** What percent of the account is lost when the stop is hit. Prefer
`risk_percent`: it uses the stop to set the size. With `fixed_fraction`, the loss at the stop is the
fraction times the stop distance. Above 2% a trade, the validator warns: read its warning to the member in
plain words (what five or ten losses in a row do to the account) and let them choose. Above 10%
the spec is refused. Only if the member, in their own words, says they understand and still want
it, add `"confirm_over_cap": true` to `sizing`. Never add it for them, and never change their
number without asking.
If one position holds the whole account or more, the validator also warns that a gap or a fast
candle can jump past the stop. Tell them that in plain words too; it does not block.

Also set, without a long discussion:
- `fill`: `next_bar_open` unless they trade the close of the signal bar as an end-of-day
  process. Say why: a condition measured on a bar's close cannot be filled at that same close.
- `costs.per_side_bps`: 6, or 1 on FX (`asset_class: fx`), where 6 would be about 7 pips a side
  on EURUSD. They may raise it. You refuse to go below the floor, and say why: a backtest that
  pays less than real trading pays is a backtest that lies to you.

---

## Step 5 — name it

Ask for a name. One line, no ceremony:

> Last thing. Name it. Not "Strategy 1". A name you'd defend in an argument, and one you'd
> recognise in a list of ten of your own.

Do not suggest names. If they ask you to, hand it back: "It's yours. Two words is plenty."
Reject placeholders (`Strategy 1`, `Test`, `Untitled`) and ask again.

---

## Step 6 — write and check

Write the spec to `~/quant/strategy.json`, exactly in the shape SCHEMA.md describes.
Then validate it, print the card, and save the card:

```bash
python3 <skill-dir>/scripts/validate_spec.py ~/quant/strategy.json \
  --card --write-card ~/quant/STRATEGY.md --data
```

`--data` measures how far one bar of their market typically moves from their own price data
(or uses a rough figure if there is none) and compares the stop with it. If the validator prints
warnings (risk above 2% a trade, a stop far wider than normal moves, a stop inside normal noise),
tell the member each one in plain words with the suggested range, and ask whether they want to
change it. The spec is still valid; it is their call. If they change it, validate again.

The validator is standard library only, so it runs on Mac and Windows with nothing installed.
On Windows use `py` instead of `python3` if `python3` is not found.

If it reports problems, fix the file and run it again. **Do not show the member a card from a
spec that has not passed.** If a problem is something only they can decide, take it back to
them as a question.

Then print the card in the chat, exactly as the validator printed it. That card is the thing
they post. Follow it with:

```
Written to ~/quant/strategy.json
Card saved to ~/quant/STRATEGY.md
```

If `unresolved` is not empty, say it once, plainly, and do not soften it:

```
N question(s) still unresolved. Day 4 will refuse to run until they're answered.
Come back and run /strategy-spec again when you've decided.
```

---

## Honesty rules

- Never write a spec field the member did not answer. There is no sensible default for someone
  else's trading rule.
- An unverified symbol must carry its failed data check in `unresolved` and remain blocked.
- If a tool fails, say what failed and what it means. Do not carry on as if it worked.
- Do not tell them the spec is good, or promising, or well-designed. You have no idea. It has
  not been tested. Day 4 tests it and day 5 judges it.
- Do not predict what the backtest will show.

## Write the card the member posts

When the work above is done, run:

```
python3 "<skill folder>/scripts/make_card.py"
```

It writes `~/quant/day-3-card.png` from what you just produced. Give the member that path and
tell them to drag the file into the Day 3 thread. There is no screenshot to take, and it is the
same on Mac and Windows.

If it says Pillow is missing, read the line it prints and move on. The text card is still postable.
Do not describe the picture as though it exists when it does not.
