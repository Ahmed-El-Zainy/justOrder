# Documentation

This directory is the **as-built** reference: what was actually built, how it
works, and why it works that way — written from the finished code and from
the incidents encountered while building it.

It's a different layer from [`specs/001-procurement-chat-assistant/`](../specs/001-procurement-chat-assistant/),
which holds the **pre-code** planning artifacts (constitution, spec, plan,
research, data model, contracts, tasks) produced with
[Spec Kit](https://github.com/github/spec-kit) before implementation began.
Read `specs/` for *what was required and why it was designed this way in
advance*; read this directory for *what exists now and what it took to get
there*. The two will agree on most facts and disagree on nothing load-bearing
— where they'd otherwise drift (e.g. Motor being replaced by
`AsyncMongoClient` after `research.md` was written), this documentation is
the current source of truth.

Start with the top-level [`README.md`](../README.md) for the project pitch,
one-command setup, and published evaluation scores. Come here for depth.

## Contents

| Document | Read this for |
|---|---|
| [`architecture.md`](architecture.md) | The system as a whole: component diagram, request lifecycle, why the pieces are split the way they are |
| [`agent.md`](agent.md) | The LangGraph agent, node by node — the core of the project. Graph shape, state, the validator, entity grounding, provider access |
| [`data-pipeline.md`](data-pipeline.md) | How the raw Kaggle CSV becomes the MongoDB collection: transformation rules, derived fields, indexes, the schema card |
| [`api.md`](api.md) | The HTTP/SSE contract: every endpoint, the full event sequence, error codes, how to write a client |
| [`frontend.md`](frontend.md) | The Angular app: the streaming client, components, the dev proxy |
| [`evaluation.md`](evaluation.md) | The golden-set harness: independence guarantee, batching, published model comparison |
| [`operations.md`](operations.md) | Running it locally, the config reference, reading `/health`, diagnosing a stuck request |
| [`decisions-and-bugs.md`](decisions-and-bugs.md) | Every real bug found by running the system, in the order it was found, plus deliberate spec deviations |
| [`video-script.md`](video-script.md) | Timed script for the 5–8 minute deliverable walkthrough video |

## Reading order

**If you're evaluating this submission**: top-level `README.md` → this
index → `architecture.md` → `agent.md` → `evaluation.md`.

**If you're extending the agent**: `agent.md`, then `decisions-and-bugs.md`
for the traps already found (several look like reasonable code until you
know what they broke).

**If something's broken right now**: `operations.md`'s diagnosis table
first; it links into the rest as needed.

**If you're reloading or changing the dataset**: `data-pipeline.md`.

**If you're building a different frontend against this API**: `api.md`,
paying particular attention to the SSE frame-separator warning — it's the
single most consequential detail in that document.
