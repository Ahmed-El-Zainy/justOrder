# Video walkthrough script

**Hard limit: 8 minutes.** Anything longer is disqualified. Target 6:45 so a slow take or a stumble
doesn't push you over. Every figure quoted below was re-verified against the actual files right
before this script was written — don't round up from memory when you record; read the numbers off
`evals/comparison.md` if you're ever unsure mid-take.

**Before you hit record:**

```bash
docker compose up -d mongo    # or: docker-compose up -d mongo
cd backend && uv run uvicorn app.main:app --port 8000     # terminal 2
cd frontend && npx ng serve                                # terminal 3
```

Then in the browser, ask **one throwaway question first** and let it finish, off-camera. The first
call after a cold start warms the vocabulary cache; every question after that is faster and more
representative. Have a second terminal ready with the pandas cross-check (below) already typed but
not yet run, and `evals/comparison.md` open in a tab in case you want to show it instead of narrating
the numbers from memory.

---

## 0:00 – 0:35 · The detail the whole thing turns on

Open on the dataset, not the app.

> "346,018 rows. But only 200,533 purchase orders — because each row is a **line item**, not an
> order. One order in here has 602 of them.
>
> So 'how many orders were placed in Q3 2014' is not a row count. Count rows and you get 29,018.
> The right answer is 18,352. That's 73% too high, and the query that produces it looks completely
> reasonable. Everything I built is shaped around not making that mistake."

Don't explain the architecture yet. Lead with the trap.

---

## 0:35 – 3:00 · Demo

**Q1 — the trap itself (0:35)**
Ask *"How many orders were created in Q3 2014?"*. While it streams, point at the live phase
indicator. Answer: 18,352. Open the derivation panel ("How this was answered").

> "There's the pipeline it actually ran — `$group` on purchase_order_number, then `$count`. Not a
> document count. And I can check that independently, right now, outside the app."

Switch to the pre-typed terminal and run it:

```bash
uv run python -c "
import pandas as pd
df = pd.read_csv('kaggle_data/PURCHASE ORDER DATA EXTRACT 2012-2015_0.csv', low_memory=False)
d = pd.to_datetime(df['Creation Date'], format='%m/%d/%Y', errors='coerce')
q3 = df[(d.dt.year == 2014) & (d.dt.quarter == 3)]
print('distinct orders:', q3['Purchase Order Number'].nunique())
"
```

> "18,352. That's pandas reading the raw CSV directly — a completely different code path from the
> one the agent used. Same number."

**Q2 — ranking with a chart (1:25)**
*"Which quarter had the highest spending?"* → 2015-Q2, bar chart, ranked quarters beneath it.

> "Charts only show up for rankings and time series — never for a single figure. A bar chart under
> one number would just be decoration."

**Q3 — follow-ups (1:55)**
*"What was total spending in Q1 2014?"*, then *"What about Q2?"*.

> "The second question is meaningless on its own — no year, no measure named. Open the panel: the
> pipeline shows 2014 and the same measure both carried forward, without me repeating either."

Then, in the same session: *"Who are the biggest suppliers overall?"* — open the panel again.

> "New topic, and the Q2 filter is gone. Carrying it over would have silently answered a different
> question than the one I asked."

**Q4 — what it does when it can't answer (2:30)**
Three fast ones, back to back:
- *"How much was spent in Q1 2019?"* → names the actual covered range instead of returning nothing.
- *"How much did Ministry of Magic spend?"* → no matching records, no invented number.
- *"How much did Corrections spend?"* → asks which of two real departments.

> "That last one — two real departments in this data both plausibly match 'Corrections'. Guessing
> either one would produce a confident, wrong answer. Asking is the only honest move."

---

## 3:00 – 4:30 · How it works

Show the agent graph diagram from `docs/agent.md`, then the code.

**The validator (3:00)** — `backend/app/agent/guards.py`

> "Every generated pipeline goes through this before it ever touches the driver. It's an allow-list
> with no model in it — a prompt can't argue with pure Python. `$out`, `$merge`, `$where`,
> `$function` are all rejected, including when they're nested inside a `$facet`."

Show `pytest tests/test_guards.py -v` passing. Then the second, independent control:

> "And separately, the app's Mongo user only has `read`. Here's a write attempt with that exact
> credential being refused. Two controls — neither is the reason the other gets to be skipped."

**Grounding (3:40)** — `backend/app/agent/vocabulary.py`

> "The database stores 'Consumer Affairs, Department of'. People type 'Department of Consumer
> Affairs'. A model guessing the literal string matches nothing and reports zero — a wrong answer
> that looks exactly like a right one.
>
> Matching happens in two passes. First, strip filler words like 'Department of' to decide who's
> even a candidate — without that step, a nonsense query like 'Ministry of Magic' was scoring 85%
> against four real departments, purely off that shared suffix. Then rank the survivors on the full
> string, because that same filler is what tells 'Transportation, Department of' apart from
> 'Transportation Commission'."

**Grounded synthesis (4:10)** — `nodes/respond.py`

> "The step that writes the final answer only ever sees the rows a query returned and the question
> — never the schema, never earlier turns. It has nothing to invent a number from. Zero rows never
> even reaches the model."

---

## 4:30 – 6:15 · Evaluation

> "I didn't want to just claim this works, so I measured it."

Show `evals/comparison.md`, or run `uv run python -m evals.run_eval --status`.

> "26 questions, covering counting, ranking, follow-ups, ambiguous names, out-of-range periods —
> everything you just saw and more. Expected answers are computed in pandas straight from the CSV,
> in a module that imports nothing from the backend. So a bug in my own transform can't quietly mark
> itself correct on both sides.
>
> On the model I'm running today — deepseek-v4-flash — the honest number is 22 out of 26. But here's
> the number that actually matters: **zero of those were wrong answers.** Every single miss was the
> assistant hitting its own time limit and correctly giving up, not returning a bad figure. Strip out
> the timeouts and it's 22 for 22.
>
> I also ran the identical 26 questions against a free model, gemma, to see what that trade-off
> costs. It landed at 21 out of 26 — close on paper — but three of those were genuinely wrong
> answers, not timeouts, and two of the three were follow-up questions. A smaller model runs out of
> room exactly where conversation context matters most."

**Latency, briefly (5:30)**

> "Each answer costs three sequential model calls — understand the question, write the query,
> write the answer. I measured the identical call take 3 seconds once and 21 seconds a few minutes
> later — that's the provider, not something I can fix by tuning a prompt. The spec I wrote targets
> answers under 15 seconds; I raised the hard ceiling to 60 rather than pretend a tighter one was
> realistic, and that gap is written down in the README, not hidden. What it's never waiting on is
> the database — Mongo execution is under half a second of every answer you just watched."

**What the eval found (5:55)**

> "The harness earned its keep — it caught four real bugs no code review found: a boolean field
> matched against the literal string 'YES' instead of true. 'IT services' grounded to an item name
> instead of a purchase category. An out-of-range period declined as off-topic instead of saying
> it's uncovered. And, the one that mattered most — the synthesizer once summed a partial slice of
> rows into a total that appeared in no actual row. That's the exact failure this whole design
> exists to prevent, and the eval is what surfaced it."

---

## 6:15 – 6:45 · Close

> "This was built spec-first — a constitution, a spec, a plan, and a task list, all in `specs/`,
> written before any code. The automated cross-check between them caught a requirement with a
> validation task but nothing implementing it, before I'd written a single line.
>
> If there's one thing to take from this: the hard part was never getting a model to write MongoDB.
> It was making the system either right, or honestly unable to answer — never confidently wrong."

---

## Recording notes

- Pre-warm with one throwaway question before recording starts. A cold first call runs
  noticeably slower than every question after it.
- Keep `evals/comparison.md` open in a tab as a fallback — if you blank on a number mid-take, cut to
  it rather than guessing.
- If you're running long, cut Q2 (the chart) first — Q1, follow-ups, and the failure modes carry the
  story on their own. The evaluation section is the one place accuracy matters more than pacing;
  don't cut it to save time.
- Don't read source files aloud line by line. Point at the two or three lines that matter and say
  why they're there.
- Say the real eval numbers (22/26, zero wrong answers) — the report and comparison files are in the
  repo, so a rounded-up or remembered-wrong figure is checkable and not worth the risk.
