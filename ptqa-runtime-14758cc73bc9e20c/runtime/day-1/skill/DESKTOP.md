# Day 1: desktop walkthrough

This is the Day 1 behavior for the Claude and Codex desktop packs. Do not load
the legacy CLI SKILL.md, check.sh or check.ps1 for this route. Do not install
an Academy skill into an app's hidden directory. The complete runtime already
lives in this project.

## Outcome

Save the member's intended rule and a real day-1-card.png showing the setup
checks. A pending or failed price check stays visible. The member posts their
own card. No automatic community posts or background trading processes.

## 1. Confirm the workspace

Read the project entry and locate runtime/shared and runtime/day-4/skill/scripts.
Resolve the member's home folder using the operating system; default outputs
to its quant child. Honour an explicitly requested rehearsal/output override.
Do not read unrelated home folders or existing credentials. If work already
exists, ask whether to resume before changing it.

Ask one question: "In one sentence, what trading idea would you like to test?"
Accept everyday words and vague answers. Do not improve the idea or ask for
strategy parameters today. Ask permission to create the workspace and save
the answer if file access has not been approved. Save their exact answer in
intent.md and read it back. Never describe a write as a read-only check.

## 2. Set up the private interpreter

Discover a working Python 3.10+ using the host's command tools. On Windows
check py -3 or python; reject a Store launcher that cannot run a version check.
If Python is absent, explain that prerequisite and use the official installer
with permission. Do not change system security, global PATH or shell profiles.

Explain that setup creates an isolated environment and may download pandas,
numpy and Pillow. With permission, run the packaged
runtime/day-4/skill/scripts/setup_env.py using that Python. Respect any explicit
--academy-home override. Use the resulting interpreter path for every command
below, including PNG generation. On Mac it ends in venv/bin/python; on Windows
it ends in venv/Scripts/python.exe. Quote paths with spaces using the actual shell.

Do not print terminal commands for the member to type when your local tools can
run them. If command permissions are declined, explain and stop at that step.

## 3. Run and show the check

Run runtime/shared/check_workspace.py --app <claude-or-codex> --workspace <quant>.
Read setup-result.json and the process exit status. Present the actual rows;
do not invent a PASS. Fix a missing dependency only with consent, then rerun.
Price data may be pending at this point. The check writes its evidence file.

## 4. Connect prices privately

Offer to connect the member's own Tiingo account, or keep prices pending today.
Do not claim a free tier supplies every market, history length or update rate.
When asked for key entry, run runtime/shared/key_entry.py in a process that can
stay alive while the member uses it. Provide the exact printed local URL.
Do not request the key in chat, clipboard tools, screenshots or command arguments.
The form masks the field and stores the key locally. For rehearsals, set
PTQ_ACADEMY_HOME to the approved isolated location before starting the process;
use that same environment for the probe. Do not read an existing real key store.

Ask the member to tell you when the form says Key saved locally. A form save
is not connection proof. Run runtime/shared/probe_data.py --source tiingo
--symbol SPY --workspace <quant>. If a different supported source is chosen,
use its explicit source argument. Inspect data-probe.json. On failure, state
that the request failed and check account access, symbol and connection with
the member. Never silently substitute demo data or another provider.

## 5. Make the card

Rerun check_workspace.py with the same interpreter, app and workspace. Then run
runtime/shared/render_setup.py --workspace <quant>. Check its exit status and
that day-1-card.png was freshly written. Open the actual image using the host's
supported file-opening control. Explain any FIX or PENDING badge honestly.
If image opening is unavailable, provide its real file path, not a fake preview.

## 6. Finish

Help the member attach day-1-card.png from quant in Skool's Community composer.
Suggested title: Day 1 - their first name. Body: one sentence about their idea.
Do not fabricate a Day 1 thread or post on their behalf. Keep keys out of cards
and posts. Close an unneeded local form process; no background work remains.
Tomorrow they return to this same project and send Start Day 2. Preserve quant.
