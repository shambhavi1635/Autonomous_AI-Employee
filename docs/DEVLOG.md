# Development Log

One entry per development step: what was built, why, files touched, problems
hit, and how they were solved. Decisions that were later reversed are kept
visible rather than rewritten -- the reasoning is the part that cannot be
reconstructed from code afterwards.

---

## Phase 0 -- Foundations

**Date:** 2026-10-11
**Goal:** A project skeleton, a dependency and secrets contract, and a
working LLM transport layer proven by a smoke test.
**Status:** Complete. All three smoke-test checks pass.

### Why this first

Everything later depends on it. Proving the model connection now means that
when the planner misbehaves in Phase 1, we already know the transport layer
is sound and can look for the bug in our own logic.

### Decisions

| Decision | Choice | Reasoning |
|---|---|---|
| LLM provider | Google Gemini (`google-genai`) | User's available API key. Isolated behind `llm/client.py` so a provider change is a one-file edit. |
| Planner model | `gemini-3.8-flash` | Gemini 3.x **Pro is unavailable on the free tier** (`limit: 0`). Flash 3.8 handles structured planning well and costs a fraction. Upgrading is one line in `.env`. |
| Fast model | `gemini-3.5-flash-lite` | Verification and small classifications do not need the planner model. |
| Model IDs | Config in `.env`, not constants | Google's IDs churn; 2.5 deprecates 2026-10-16. `list_models()` discovers what the key can actually use. Pinned exact IDs rather than `-latest` aliases so behaviour cannot shift without a deliberate change. |
| Orchestration | ~~None -- hand-rolled loop~~ **LangGraph** | Reversed; see below. |
| Structured output | Pydantic schema passed to the API | Constrains decoding to valid JSON. No regex parsing of model prose. |
| Retry policy | Transient errors only, **and only when the server's `retryDelay` is under 30s** | See "Retry classifier" below. |
| Python version | 3.12 (conda env at `venv/`) | Env defaulted to 3.14.8. Nothing here needs >3.12, and PyTorch wheels lag new releases, so newer was pure risk for no benefit. |
| Dependencies | All installed up front, minus three | User preference. `torch`, `sentence-transformers`, `chromadb` deferred to Phase 4 after download failures. |
| First use case | Invoice -> ledger | Pure library calls, fully verifiable by reading state back, richest demo. |
| Email approach | Gmail IMAP/SMTP, not Outlook GUI | Reliable, and reading the Sent folder makes sends genuinely verifiable. |
| Build order | One file at a time, commit each | User feedback after an up-front folder skeleton was created. See "Scaffolding removed". |

### Files

| File | Purpose |
|---|---|
| `.gitignore` | Excludes `.env`, `venv/`, generated `data/`. |
| `requirements.txt` | Full dependency set, one per line. |
| `core/config.py` | Single place resolving the API key, model IDs and retry limit. |
| `llm/client.py` | Gemini adapter: retries, structured output, token accounting. |
| `scripts/smoke_test_llm.py` | Proves model access, text generation and structured extraction. Asserts on extracted values. |
| `docs/DEVLOG.md` | This file. |

### Architectural boundaries

- `llm/client.py` is the only file that knows Google exists.
- `core/config.py` is the only file that reads the environment.
- Dependencies point one way: `llm/` imports `core/config`, never the reverse.
- `llm/client.py` holds no prompts and no concept of a Plan.

### Decision reversed: orchestration framework

**Original:** no framework, hand-rolled executor loop.
**Revised:** LangGraph from Phase 1. **Raised by:** user.

The original argument -- that a framework hides the planner, executor,
approval gate and retry logic -- holds for LangChain and CrewAI, which
supply agents. It does not hold for LangGraph, which supplies a state
machine: every node is a plain function we write. No reasoning is hidden
because none of it belongs to LangGraph.

Two concrete wins over the original design:

1. **Durable human-in-the-loop.** `interrupt()` persists graph state to
   SQLite and halts; `Command(resume=...)` continues from that point. The
   original used a worker thread blocking on an in-memory event, so a
   pending approval died with the process. (`interrupt()` requires a
   checkpointer -- it errors without one.)
2. **Streamlit rerun immunity.** Streamlit re-executes the script on every
   interaction; the original workaround was a thread publishing to a queue.
   With a checkpointer, agent state lives outside the script.

Also removes the need for a hand-built event bus: LangGraph streams node
events, which will feed the live-execution view.

Scope held deliberately: added `langgraph` and `langgraph-checkpoint-sqlite`
only -- no `langchain`, no LangChain model wrappers. `llm/client.py` is
unchanged; nodes are plain functions that call it directly.

Accepted costs: another abstraction to learn and explain; LangGraph's API
has churned, so versions are pinned; debugging through a framework is harder
than a `while` loop, mitigated by keeping nodes as pure testable functions.

### Scaffolding removed (user feedback)

Phase 0 originally created the full folder skeleton up front
(`core/ llm/ tools/ memory/ ui/ docs/ data/ sandbox/ tests/ scripts/`).
User objected: empty folders for unstarted phases are the "unnecessary
abstraction" the project rules forbid, and they imply structure no code uses.

**Rule adopted:** create a folder or file only when the code being written
at that moment needs it, then commit it.

`core/config.py` was trimmed on the same grounds -- `CHECKPOINT_DB`,
`MAX_STEPS`, `RUN_TIMEOUT_SECONDS`, `DATA_DIR`, `SCREENSHOT_DIR`,
`ALLOWED_WRITE_PATHS` and `ensure_dirs()` were removed. Each returns in the
phase that reads it.

### Problems encountered

1. **`venv/` is not a venv.** It is a conda *prefix* environment, so the
   interpreter is at `venv/python.exe`, not `venv/Scripts/python.exe`.
   Created on Python 3.14.8 because `conda create -p venv python` pins no
   version. **Resolved:** recreated with `python=3.12`; now 3.12.15.

2. **Packages installed into the wrong environment, twice.**
   First into conda base, then into a *different* environment also named
   `venv` at `miniconda3/envs/venv`. Root cause: `conda activate venv`
   matches by **name**, and a prefix environment has no name -- so it
   silently activated an unrelated global env, and bare `pip` followed it.
   **Resolved:** never activate; always `venv\python.exe -m pip ...`, which
   is a file path and cannot be ambiguous.

3. **`torch` download failed.** Repeated
   `Failed to resolve 'files.pythonhosted.org'` (DNS), plus a `[WinError 32]`
   file lock caused by two pip processes running concurrently.
   **Resolved:** deferred `torch`, `sentence-transformers` and `chromadb` to
   Phase 4. If torch still fails then, the memory layer falls back to SQLite
   + numpy cosine similarity behind the same interface.

4. **A pipeline hid a failed install.** `pip ... | tail -25` reported exit 0
   because the exit code of a pipeline is the *last* command's. The install
   had actually failed. **Lesson:** do not pipe a command whose exit status
   matters.

5. **`venv/python.exe` not recognised in cmd.** The VS Code terminal runs
   `cmd.exe`, where `/` introduces a switch, so `venv/python.exe` parsed as
   the command `venv` with option `/python.exe`. Use backslashes in cmd.

6. **Gemini 3.x Pro is unavailable on the free tier.** `429
   RESOURCE_EXHAUSTED ... limit: 0, model: gemini-3.1-pro` -- not an
   exhausted allowance but a zero allowance. **Resolved:** planner switched
   to `gemini-3.8-flash`, which passes all checks.

7. **Retry classifier too naive** (found via problem 6). A 429 was always
   treated as transient, so a quota error with `retryDelay: 57464s` (16
   hours) was retried three times before failing -- burying Google's actual
   message under "failed after 3 attempts".
   **Fix:** `_is_transient()` now also parses `retryDelay` from the error
   body and refuses to retry anything longer than 30s. Covered by 8 cases
   including the real error text; all pass.
   **Generalisable lesson:** the same status code can mean opposite things,
   and the server usually says which in the body.

### Verification

`scripts/smoke_test_llm.py` -- all three checks pass:

- 45 models visible; both configured IDs resolve
- text generation returns `READY`
- invoice extraction returns `total_amount=2540.5`, `line_item_count=3`,
  `currency=USD`, all asserted
- cost: 2 API calls, 157 in / 47 out tokens

### Known gaps

- The 8 retry-classifier test cases were run ad hoc, not saved. They belong
  in `tests/` once there is a second thing worth testing.
- `requirements.txt` still lists `torch`, `sentence-transformers` and
  `chromadb`, which are not installed -- a fresh `pip install -r` will fail
  the same way. Either comment them out or accept it knowingly.
- A benign SDK warning about automatic function calling appears on every
  `generate_content` call. Cosmetic.

### Next

**Phase 1 -- skeleton execution loop.** In order:
`core/schemas.py` (typed `Plan` / `Step` / `AgentState`), `tools/registry.py`
(the `@tool` decorator), two or three trivial tools, `core/planner.py`, then
the first LangGraph graph with a working `interrupt()` approval gate.

**Success criterion:** "read a file and tell me the third line" is planned,
approved in the terminal, executed and verified -- *and* the run survives a
process restart between approval and execution, proving the checkpointer.
