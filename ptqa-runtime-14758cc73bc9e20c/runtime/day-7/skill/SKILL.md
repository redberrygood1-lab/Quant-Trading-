---
name: week-one-card
description: Builds the member's Part-Time Quant Academy week-one summary card by reading the outputs of days 3 to 6 in ~/quant — the strategy spec, the backtest result, the honest verdict and the paper-loop journal — and prints a card they can screenshot and post, plus the five-line partner intake block used to match them with a build partner. Use when the member says "build my week one card", "week one summary", "week 1 report", "day 7", "my academy card", or asks what their agent did this week.
allowed-tools: Read, Glob, Grep, Bash, Write
---

# Week-one card

Day 7 of the Part-Time Quant Academy. The member has spent six days turning an instinct into
a tested rule. This skill reads back what actually happened, in their own numbers, and gives
them one thing to post.

## Three hard rules

1. **Never invent a number.** Every figure on the card comes out of a file the member
   produced on days 3 to 6. If a file is missing or unreadable, the card says `not found` on
   that line and the notes say which day to re-run. A card with a made-up Sharpe on it is
   worse than no card.
2. **`could not run` is not `not found`, and neither is a pass.** Day 5 reports a check it
   could not run as an explicit null. The card prints `[N/A ]` and the reason, and the notes
   repeat that a check which could not run is not a check that passed. Never round that up.
3. **Never turn a pass into permission.** `APPROVE` means the rule survived the gates on
   historical data. It does not mean trade it with money. The footer says so and the footer
   is not optional.

## What it reads

The week lives in the folder day 1 created:

```
Mac / Linux   ~/quant
Windows       C:\Users\<you>\quant
```

| Day | File | Shape | What the card takes |
|---|---|---|---|
| 3 | `~/quant/strategy.json` | strategy spec v1, `day-3/skill/schema.json` | `name`, `instrument.symbol`, `timeframe.bar`, `entry.plain_english`, `costs.per_side_bps` |
| 4 | `~/quant/backtest-result.json` | `day-4/skill/reference/SPEC-CONTRACT.md` | `data.start`, `data.end`, `data.bars`, `metrics.trades`, `methodology.costBpsPerSide` |
| 5 | `~/quant/verdict.json` | schema `ptq-academy/verdict/1` | `engineVerdict`, the whole `gates[]` array, `pboPct`, `deflatedSharpe`, `walkForward`, `configurationsTried` |
| 6 | `~/quant/run-state.json` + `~/quant/run-log.jsonl` | `day-6/skill/reference/OUTPUTS.md` | `loop.cycle_count`, `kill_switch.engaged`, `kill_switch.engaged_at`, journal `open`/`close`/`kill-switch-engaged` events |

Day 6 writes into the workspace in the open, next to days 3 to 5 — nothing this week lives
in a hidden folder. The script also checks `PTQ_ACADEMY_HOME` (default
`~/.ptq-academy/day-6`, files named `state.json` and `journal.jsonl`) as a fallback, for
members who ran an earlier build of day 6.

**Gates come from day 5, not from here.** The card renders day 5's `gates[]` array verbatim —
its labels, its values, its thresholds, its pass/ran flags. No threshold is hardcoded in day
7, so if day 5 changes one, the card follows without an edit.

**No performance figures.** `metrics.netReturnPct`, `sharpe`, `profitFactor` and the equity
curve are all sitting in `backtest-result.json` and none of them go on the card. The card
carries the period, the trade count, the cost model and the verdict. Do not add a return
figure even if the member asks.

If a member kept their week somewhere else, the script falls back to searching by filename
keyword, and flags in the notes that a file was matched by name rather than found where it
was expected. `--dir` overrides everything.

## Steps

### 1. Ask the one question

Before running anything, ask exactly this and wait:

> In one sentence, what have you learned since starting Day 1?

Do not suggest an answer. Do not tidy their wording beyond removing a trailing full stop.
That line is the only part of the card they write.

### 2. Choose the next step and offer partner details

Ask what one thing they want to investigate next. Keep their answer verbatim as
the week-two goal, including finishing an incomplete day if that is their choice.
Do not start another test or change their strategy as part of this review.

Then ask whether they want to include partner details in their community post.
This is optional. If yes, collect the remaining fields below in one go. The goal
is already answered; do not make them answer it again. These details request an
introduction, not a confirmed match. Never claim an automatic matching service
has run without actual evidence.

- **Timezone** — a city or a UTC offset. `Europe/London`, `America/Chicago`, `UTC+5:30`.
- **When they can show up** — days and a realistic length. "weekday evenings, 30 min".
- **Week-two goal** — one line, concrete. "rewrite the exit so it can't see the future".
- **Experience** — one line. "3 years discretionary, no code".

Verdict is the fifth line and comes from day 5 automatically. If they skip a field,
leave it blank rather than filling it in. If they decline partner details, omit
the optional intake from their proposed post. Still save their chosen next step
with the report.

### 3. Build the card

```bash
python3 scripts/build_card.py \
  --name "Sam R." \
  --learned "that my rule only worked because I was reading the exit after the fact" \
  --tz "Europe/London" \
  --hours "weekday evenings, 30 min" \
  --next "rewrite the exit so it can't see the future" \
  --experience "3 years discretionary, no code"
```

**On Windows run it as one line, and use `python`.** PowerShell does not accept `\` as a
line continuation, and Windows ships a fake `python3` that opens the Microsoft Store rather
than running anything — day 1's check covers turning that alias off. Do not put `~` in a
path you hand to `python` on Windows either; PowerShell leaves it literal in arguments.
Use `$env:USERPROFILE\...`.

If `python` does not work either, do **not** tell the member to go and install Python. Fall
back to step 3b.

The script is standard library only. It needs no numpy, no pandas and no internet.

Optional flags: `--dir <path>` for a workspace that isn't `~/quant`, `--json` to also write
`week-one-card.json`, `--width 68` to change the card width.

### 3b. Fallback — no working Python

Do it yourself with the tools you have:

1. `Read` `~/quant/strategy.json`, `~/quant/backtest-result.json`, `~/quant/verdict.json`,
   `~/quant/run-state.json` and `~/quant/run-log.jsonl`.
2. Pull the fields in the table above. Render day 5's `gates[]` in order, marking each
   `[PASS]`, `[FAIL]` or `[N/A ]` from its `pass` and `ran` flags.
3. Match the layout in `reference/CARD.txt` — same widths, same footer.
4. `Write` the result to `~/quant/week-one-card.txt`.

Same rules apply: anything you cannot find from a file is `not found`, never a guess, and no
return figures.

### 4. Show it and stop

Print the card, print the partner block, and give them exactly two instructions:

- Screenshot the card.
  - **Mac** — `Cmd`+`Shift`+`4`, drag a box. The picture saves to their **Desktop** as
    `Screenshot <date> at <time>.png`. A screenshot app such as CleanShot catches the same
    shortcut and shows its own overlay instead, in which case they use its copy or save.
  - **Windows** — `Windows`+`Shift`+`S`, drag a box. It goes on the **clipboard**, so they
    paste it into the post with `Ctrl`+`V`. Windows 11 also keeps a copy in
    `Pictures\Screenshots`.
  - The card is written to `~/quant/week-one-card.txt` as well, so a member who cannot get
    a screenshot working can paste the text instead.
- Post it in the week-one thread with the five lines under it, using the template in
  `ACTION.md`.

Then stop. Do not offer to improve their strategy, do not offer to re-run the backtest, and
do not suggest what they should build next. Day 7 ends with a post and a hello.

## If nothing is found

If the script cannot find `~/quant` at all, the member moved it. Ask:

> Where did you save this week's work? Give me the folder and I'll read it from there.

Then re-run with `--dir`. Do not build a card from conversation memory. If they genuinely
never produced day 5, say so plainly: the card prints without a verdict, but the verdict is
the point of the week, and day 5 is worth going back for.

## Tone

Flat and factual. No congratulating. A `BLOCK` verdict is the ordinary outcome, and the card
should not read as a commiseration when it prints one. The member found out their rule does
not hold up, which is the thing they came for.

## Write the card the member posts

When the work above is done, run:

```
python3 "<skill folder>/scripts/make_card.py"
```

It writes `~/quant/day-7-card.png` from what you just produced. Give the member that path and
tell them to drag the file into the Day 7 thread. There is no screenshot to take, and it is the
same on Mac and Windows.

If it says Pillow is missing, read the line it prints and move on. The text card is still postable.
Do not describe the picture as though it exists when it does not.
