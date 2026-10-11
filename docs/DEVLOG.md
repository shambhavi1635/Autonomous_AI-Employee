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

---

## Phase 1 -- Skeleton execution loop

**Date:** 2026-10-11
**Goal:** A task goes in, a plan comes out, a human approves it, the steps
run, each one is verified, and the run survives the process dying.
**Status:** Complete. Success criterion met, including restart survival.

### Files, in build order

| File | Purpose |
|---|---|
| `core/schemas.py` | `Plan`, `Step`, `StepResult`, `Risk`. The contract every component agrees on. |
| `tools/registry.py` | The `@tool` decorator: catalog, argument schema, validation and risk tier from one source. |
| `tools/files.py` | `list_files`, `read_file`, `write_file`, confined to `sandbox/`. |
| `core/planner.py` + `llm/prompts/planner.md` | Task to validated plan, checked against the real registry. |
| `core/executor.py` | Resolve placeholder arguments, invoke, verify the postcondition. |
| `core/graph.py` | The LangGraph state machine and both approval gates. |
| `app_cli.py` | Terminal front end: approve / edit / reject, list runs, resume. |

### Design decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Planned vs observed | `Plan`/`Step` come from the model; `StepResult` only from our code | If the model could write its own results, "did it work?" would be judged by the thing being checked. |
| `ok` vs `verified` | Two separate fields; `succeeded` requires both | A tool can return cleanly and still achieve nothing. |
| Risk source of truth | The tool registry, never the plan | A plan is untrusted input and can be influenced by documents the agent reads. `effective_risk()` lets a plan raise a tier, never lower it. |
| Tool arguments in a plan | `arguments_json: str`, parsed by a property | A `dict[str, Any]` compiles to `additionalProperties`, which the Gemini Developer API rejects outright. Nothing is lost: arguments are validated against the tool's own schema, which is stricter. |
| Unknown tool arguments | `extra="forbid"` on every derived model | Pydantic ignores unknown fields by default, so a plan passing a misspelled argument would otherwise run as if nothing were wrong. |
| Placeholder resolution | A model call, not string substitution | `list_files` returns ten names when the next step needs one; `read_file` returns a document when the intent wants one line. Lets the approved plan stay readable. |
| Verification | Two cheap calls: choose a READ probe, then judge its output | Asking "did it work?" from the tool's own return value lets the model agree with itself. A fresh read makes the verdict rest on independent evidence. |
| Probe safety | Only `READ` tools may probe; enforced in code | A check must never change the thing it is checking. |
| Plan validation | Against the live registry, before the human sees it | A schema-valid plan can still name a tool that does not exist. |
| Repair attempts | One | If the model cannot fix a plan after being shown the exact errors, another attempt will not help. |
| Gate ordering | Resolve arguments *before* gate 2 | Gate 1 approves an intention; gate 2 approves the actual payload. |
| CLI edit | Opens the plan as JSON in an editor | Supports every edit with no bespoke UI, and the result is validated like any other plan. |

### Problems encountered

8. **`str.format()` silently broke the placeholder syntax.** The planner
   prompt teaches the model to write `{{step_N.output}}`, but `.format()`
   converts `{{` into `{`, so the model was being taught single braces
   while the parser looked for double. The mechanism would never have
   matched, and the symptom would have been baffling: plans that look
   correct and quietly fail to pass data. **Fix:** plain `.replace()`.
   **Lesson:** never run a brace-heavy prompt through a brace-based
   templating function.

9. **The verifier confabulated.** Step 2 wrote the correct third line, and
   the verifier failed it, claiming the text was "the first line of
   notes.txt". The judge had been shown `out/third.txt` but never
   `notes.txt`, so it could not know which line it was -- and instead of
   saying so, it invented a confident wrong answer. **Fix:** pass earlier
   steps' outputs to the judge, and state in the prompt that "cannot tell"
   is an acceptable verdict and better than a guess.
   **Lesson:** a verifier is just another model call, and needs the same
   scepticism as the thing it verifies.

10. **Gate 2 showed arguments the human could not read.** The approval
    panel displayed `body: '{{step_1.output}}'`. Approving a payload you
    cannot see is not approval. **Fix:** resolve arguments before the gate.
    Resolution now runs twice per irreversible step, because a node is
    re-entered from the top on resume -- acceptable, since resolving only
    reads. Had the tool call sat before the gate instead, approving would
    have sent two emails.

11. **A real step failure, caught by verification.** A resumed run
    extracted line 2 instead of line 3. `write_file` reported success, and
    the run still failed because `verified` was false. Recorded as evidence
    that the verification layer earns its cost on real mistakes, not just
    in principle.

12. **Model choice reversed by measurement.** Problem 11 looked like the
    lite model being weak at counting, so resolution was switched to
    `PLANNER_MODEL`. Re-running the same resolution five times gave 5/5
    correct on the lite model. The failure was nondeterminism, not
    capability, so the change was reverted; the real answer is the retry
    ladder. **Lesson:** one LLM failure is an anecdote. Sample before
    believing a pattern.

13. **Free-tier quota is 20 requests per day on the newest models.**
    `gemini-3.8-flash`, and `gemini-flash-latest` which points at it, were
    exhausted by one day of testing; both return a 429 with a ~13 hour
    `retryDelay`. Still available with room: `gemini-3.5-flash`,
    `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-3.6-flash`.
    **Fix:** `PLANNER_MODEL=gemini-3.5-flash`; `FAST_MODEL` unchanged.
    The Phase 0 retry fix proved itself here -- the error surfaced
    immediately instead of after three pointless retries.
    **Planning note:** a two-step run costs about 5 calls, only one of
    which is planning. Later phases will make more; enabling billing on
    the Google project removes the ceiling if it becomes limiting.

### Verification

End to end, through the CLI:

- **Approve** -- correct file written, both steps verified.
- **Reject** -- run stopped, `notes.txt` untouched.
- **Gate 2** -- execution paused mid-run before `send_email`; `abort` gave
  zero emails sent; `approve` showed the real resolved body first.
- **Restart survival** -- process 1 planned and paused at the gate, then
  exited without approving, leaving the sandbox untouched. A separate
  process read the run from a 20KB SQLite checkpoint, presented the same
  gate, and completed the task correctly.
- **Plan validation** -- rejected a nonexistent tool, a misspelled
  argument, a forward reference and a self-reference.
- **Sandbox** -- four traversal attempts blocked, including
  `notes/../../../../secrets.txt`.

### Known gaps

- **No retry or repair.** `after_step()` stops on the first unverified
  step. The ladder (retry, reflect, re-plan, escalate) belongs in that one
  function -- which is the main argument for a graph over a `while` loop.
- **Still no `tests/`.** Everything above was verified by hand at the time
  it was written. Those checks belong in pytest files.
- **Email postconditions are not independently verifiable** until there is
  a Sent folder to read. The verifier can only confirm the tool returned.
- **`MAX_STEPS` is checked, but there is no wall-clock timeout** and no
  loop detection (same tool, same arguments, twice in a row).
- Carried over from Phase 0: `requirements.txt` lists three packages that
  are not installed, and the SDK's automatic-function-calling warning
  still prints on every call.

### Next

**Phase 2 -- the real use case.** `tools/documents.py` (PDF extraction),
`tools/ledger.py` (a stand-in for the internal system), and the invoice to
ledger task working end to end. Plus the retry ladder, which problem 11
showed is the missing piece rather than a nicety.
