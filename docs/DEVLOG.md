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

---

## Phase 2 -- Invoice to ledger, and failing well

**Date:** 2026-10-11
**Goal:** The first real use case end to end -- find an invoice PDF, read
it, file it in an internal system, and confirm it is there. Plus the
failure handling Phase 1 showed was missing.
**Status:** Working. Invoice to ledger verified on two different invoices,
including one designed to be hard.

### Files

| File | Purpose |
|---|---|
| `tools/ledger.py` | SQLite invoice store: `add_invoice`, `find_invoice`, `list_invoices`. |
| `tools/documents.py` | `read_pdf` -- text extraction only, no interpretation. |
| `scripts/make_sample_invoices.py` | Four invoice PDFs, deliberately different layouts. |
| `core/executor.py` | `repair_step()` and `verify_goal()`. |
| `core/graph.py` | `repair` and `replan` nodes; goal check in `finalise`. |
| `tests/` | 131 offline tests. |

### Design decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Invoice extraction | **No extraction code at all** | The obvious `extract_invoice_fields()` would need a model call inside `tools/` (breaking the rule that tools stay testable without an API key) and would be an invoice-shaped workflow -- the hardcoding this project exists to avoid. `read_pdf` plus placeholder resolution already does it, and the same two steps work for a receipt or a contract. |
| Sample invoices | Four vendors, four layouts | One shared template would let memorisation pass as extraction. They differ in what the number is called, date format, what the total is called, currency, and structure. Globex carries a subtotal and a GST line directly above the real total, as a trap. |
| Ledger realism | Real SQLite with constraints | The point of the use case is that the agent's work can be checked by something other than the agent. |
| Unique invoice numbers | Enforced by the schema | Agents retry. Without it, a retry after a timeout silently creates a second payment record. |
| `add_invoice` risk | `WRITE`, not `IRREVERSIBLE` | A row can be deleted. In a real system that triggers payment it would be `IRREVERSIBLE` -- a one-word change, which is the point of declarative risk. |
| Date handling | **Refuse** non-ISO dates | `02/10/2026` is 2 October or 10 February depending on the country. A tool that guessed would file wrong records that look fine. Refusing pushes the decision back to the step that can still see the document. |
| Where constraints live | In the tool, enforced | Not in the prompt, hoped for. A prompt is a request; a tool is a gate. |
| Repair vs retry | Repair, not blind retry | Blind retries only help with transient faults. Most agent failures are the agent having been wrong, and repeating the same call repeats the mistake. Tools that explain themselves ("not UTF-8 text -- use read_pdf") make repair possible. |
| Re-plan scope | Whole plan, from scratch | Splicing a new plan onto half-finished state would leave placeholders pointing at step numbers from a plan that no longer exists. Costs re-doing earlier reads; buys internal consistency. |
| Re-plan approval | Always back to the human | The agent may change its mind; it may not change what you approved without asking. |
| Test suite | Entirely offline | A suite needing credentials gets skipped, and one costing money per run gets run less often than it should. Model judgement is checked by hand against the sample invoices; everything that must hold regardless of what the model says is tested. |

### The failure ladder

    step fails -> repair   (fix this step, max 2 attempts)
               -> replan   (the plan itself is wrong, max 1)
               -> stop and tell the human

Three safeguards on repair: risk is re-derived from the replacement tool,
so a repair cannot swap in something dangerous and skip the gate; a repair
proposing the identical call is rejected; and the repaired step replaces
the original in the plan, with `message` recording the swap, because a
plan that silently rewrote itself would undermine approving one.

### Problems encountered

14. **Typed placeholders.** `_stub_placeholders` substituted the string
    `"placeholder"` when type-checking, which failed `total_amount: float`.
    So a *correct* plan was rejected, and the repair round then pushed the
    model to replace the placeholder with a literal it invented -- a
    hardcoded `0.0` instead of the real total. **Lesson:** a validator that
    rejects correct input is worse than no validator, because the system
    works around it in a way that looks like success.

15. **The catalog shows only a docstring's first paragraph -- hit twice.**
    The "not for PDFs" warning on `read_file` and the ISO-date requirement
    on `add_invoice` were both in paragraph two, invisible to the planner.
    **Lesson:** the first line of a tool's docstring *is* the prompt.
    Anything needed when *choosing* the tool belongs there.

16. **The verification probe hallucinated a filename.** Asked how to check
    a step without being told what the step had touched, it invented
    `acme_invoice.pdf`. Same root cause as problem 9: a model asked to
    decide without the information needed to decide. **Fix:** the probe now
    sees the real call with its resolved arguments.

17. **The planner copied values out of the task.** Asked to file "the Acme
    invoice", it hardcoded `vendor="Acme"` instead of reading the vendor
    from the document, filing `Acme` where the invoice said
    `ACME CORPORATION`. **Fix:** prompt rule 3b -- words in the task
    identify *which* thing to find; they are not the data.

18. **Right steps, wrong thing -- the most serious failure so far.**
    Asked to file the **Wayne Enterprises** invoice, which does not exist,
    the agent listed the folder, read `acme.pdf`, filed ACME CORPORATION,
    and reported *"All 3 steps completed and verified."* Every step was
    honest. Every postcondition held. The postconditions asked whether *an*
    invoice had been filed, not whether *that* one had.

    No per-step check can catch this, because no step is wrong. The hole
    was structural: the original design called for three levels of
    verification and only two had been built.

    **Fix:** `verify_goal()` -- a final check comparing the original
    request against the whole trace, run in `finalise` after every step has
    already passed. It is told explicitly that "could not find what was
    asked for" is a legitimate outcome and substituting the nearest
    available thing is not. Plus prompt rule 2b: when a request names a
    vendor, file or record, the postcondition must name it too.

    **Lesson:** this is the failure that matters in a business setting --
    not crashing, but confidently doing the wrong thing. Verification has
    to reach all the way up to the request, or it verifies the agent's
    interpretation rather than the user's intent.

### Verification

- **Acme invoice** -- filed correctly: number, date, amount, currency.
- **Globex invoice** -- the hard one. Correct vendor, number and currency,
  and it picked 98,400.00 as the total rather than the 83,389.83 subtotal
  or the 15,010.17 GST line sitting above it.
- **Repair ladder, in a real run** -- `attempts: {'3': 2}`. Step 3 was
  rejected for a non-ISO date, the agent read its own error, converted
  `02/10/2026` to `2026-10-02`, retried, and passed. No human involved.
- **Repair, isolated** -- `read_file` on a PDF -> error -> repaired to
  `read_pdf` with the same path -> retry succeeded.
- **Tests** -- 131 passing in under 5 seconds, no API key required.

### Known gaps

- ~~`verify_goal` unproven live~~ **Proven.** The Wayne Enterprises run
  now fails correctly, and every rung fired in one run: step 1's
  postcondition named the entity and failed ("the listed files do not
  contain any Wayne Enterprises invoice"), repair was exhausted, the plan
  was redone and re-approved by the human, the new plan's single step
  verified cleanly -- and `verify_goal` still failed the run, because
  listing files is not filing an invoice. Ledger empty throughout.
- **No LLM response cache.** Most quota today went on re-running the same
  task while fixing bugs, which also made failures hard to reproduce --
  the line-2-vs-line-3 bug disappeared on re-run. A record/replay cache in
  `_call()` would make development cheaper and debugging deterministic.
- **No wall-clock timeout, no loop detection.**
- Three packages in `requirements.txt` are still not installed.

### Next

**Phase 3 -- the UI.** Streamlit: the plan as an editable table, a live
execution feed, a stop button, run history. The graph needs no changes --
`app_cli.py` was kept free of agent logic precisely so a second front end
could drive the same machine.

### Addendum: two more problems, found closing the Phase 2 gaps

19. **Truncated structured output read as a schema mismatch.** The lite
    models stopped part-way through a plan, and the error said "Model
    output did not match Plan" with JSON cut off mid-sentence -- which
    points at the wrong cause. **Fix:** set `max_output_tokens`
    explicitly (the default left no room for a multi-step plan plus the
    model's thinking tokens), and detect truncation when reporting, so
    the message says "cut off" rather than "malformed".
    **Fix 2:** `generate_structured` now makes two attempts, with a
    slightly higher temperature on the second to move off a bad sample.
    The same prompt failed twice and then succeeded unchanged, which is
    exactly the case one retry handles.

20. **A second API key does not grant fresh free quota.** Tested
    directly: a newly created key showed the same exhausted 20/day limit
    on `gemini-3.5-flash`. Google tracks the free tier per project or
    account, not per key. Recorded because it settles the question with
    evidence rather than assumption. Each *model* does have its own
    bucket, so pointing `PLANNER_MODEL` and `FAST_MODEL` at two
    different models spreads load legitimately. The real fixes remain
    enabling billing, and caching LLM responses during development.

---

## Phase 3 -- The user interface

**Date:** 2026-10-11
**Goal:** A browser UI: task entry, the plan as an editable table, both
approval gates, a live execution feed, results, run history.
**Status:** Working, driven end to end in a browser.

### Files

| File | Purpose |
|---|---|
| `app.py` | The Streamlit front end. |
| `tools/report.py` | `report(answer)` -- how the agent answers a question. |
| `sandbox/expenses.csv` | A second demo file, so the system is visibly not invoice-only. |
| `.claude/launch.json` | Run configuration for the dev server. |

### The architectural payoff

`core/` was not touched. Two front ends -- `app_cli.py` and `app.py` --
drive the same compiled graph, because `app_cli.py` was deliberately kept
free of agent logic in Phase 1. The boundary cost nothing to maintain and
paid for itself here.

### Design decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Streamlit's rerun model | Ignored it | Streamlit re-executes the script on every interaction, which usually forces a background thread and a queue. The graph checkpoints itself, so a rerun just reloads. The only thing kept between reruns is which run is being viewed. |
| Where gates come from | The checkpointed snapshot, not the last call's return | `pending_gate()` reads `snapshot.tasks[].interrupts`. That is what lets a refreshed browser -- or a different session tomorrow -- pick up a paused run exactly where it stopped. |
| Live feed | `graph.stream()` | Yields after each node, so the page fills in as the agent works. No threads. |
| Plan editing | A table, validated on approval | Edits are re-validated exactly like a model-written plan, because a human edit can break one just as easily -- a deleted step a later one referenced, a tool name typed wrong. |
| Abandoned runs | A "Continue" button | Navigating away ends the Streamlit request but not the run, since its state is checkpointed. Without this the UI offered no way back in. |
| Answering questions | A `report` tool | See problem 23. |

### Problems encountered

21. **Invalid Streamlit icons.** `st.error(msg, icon="!")` raises --
    Streamlit wants an emoji or a `:material/name:` shortcode. Found in
    the first thirty seconds of actually running the app, which is the
    argument for running it rather than reasoning about it.

22. **A wrong diagnosis, corrected by measuring.** Plans were failing with
    "Model output did not match Plan", and the raw output in the error
    appeared to stop mid-sentence, so the conclusion was truncation and
    `max_output_tokens` was added. Checking properly: `finish_reason` was
    `STOP` and the JSON was complete and well formed. **The "cut off"
    output was this project's own 1500-character slice in the error
    message** -- a misread of our own debug output.

    The real cause: the model wrote `"total_amount": {{step_2.output}}`
    with the placeholder **unquoted**, which is not valid JSON. It had
    been told placeholders may fill numeric fields and dropped the
    quotes. **Fix:** repair it in `Step`'s validator -- the malformation
    is specific and the intent unambiguous, so a mechanical fix beats
    asking the prompt more firmly, which works only most of the time.
    Four regression tests. The misleading comment in `config.py` was
    corrected rather than left to mislead someone later.

23. **The agent could not answer a question.** Asked to "show the second
    entry of expenses.csv", it read the file and reported success -- with
    the whole file as the output. The answer was in there, buried, and
    nothing singled it out. Every other tool changes or inspects the
    world; none could *say* anything, so a task could be executed
    perfectly and leave the person who asked no better off.
    **Fix:** `report(answer)`, a tool that returns what it is given. The
    work is nothing; the value is that argument resolution fills `answer`
    from earlier outputs guided by the step's intent, so the relevant
    part is extracted rather than passed through whole. Plus planner rule
    4b and an Answer block in the UI.
    **Worth noting:** adding an entirely new category of capability --
    communication -- cost one decorated function and one prompt rule. No
    change to the planner, executor, graph or schemas.

24. **The goal verifier wrongly failed a correct run.** A Globex invoice
    was filed correctly, and `verify_goal` reported "the steps only
    located and verified the invoice without performing the filing
    action". It had been given only the `evidence` lines -- which
    describe the *verification probe*, and probes are read-only by
    design. Step 3's evidence read "find_invoice returned the record",
    so the trace looked like a sequence of lookups.
    **Fix:** the trace now states which tool each step actually ran,
    alongside what was observed. Confirmed by re-running the judge on the
    same stored run: `False` before the fix, `True` after.

    **This was the third instance of one pattern.** Problem 9: the step
    judge could not see the earlier output to compare against. Problem
    16: the probe did not know which file the step had touched. Problem
    24: the goal judge did not know which tools had run. Each time the
    model invented a confident answer rather than saying it could not
    tell. The lesson is not about prompts: **when a model judges, audit
    what it can actually see.** The instinct is to reword the prompt; the
    fix is almost always the inputs.

25. **Step outputs were not shown at all.** The results table showed the
    evidence column, which describes verification rather than the result.
    For a question-shaped task the UI reported success while displaying
    nothing the user had asked for. **Fix:** `render_outputs()` -- a list
    of records renders as a table, structures as JSON, text as a code
    block.

26. **Streamlit does not reload imported modules.** A fix to
    `core/schemas.py` had no effect because the running server still held
    the old module in `sys.modules`. The same bug appeared to persist
    after being fixed, which is a particularly wasteful way to lose time.
    **Rule:** restart the server after changing anything outside
    `app.py`.

27. **A disabled button lagged a rerun behind.** `disabled=not
    task.strip()` is only re-evaluated on the rerun that typing triggers,
    so the first click after typing hit a still-disabled button and did
    nothing. **Fix:** check the value on click instead.

### Verified in a browser

- Task entry, plan table, approve -> live per-step feed -> results.
- The Acme and Globex invoices both filed correctly; Globex via a repair
  that converted `02/10/2026` to ISO after the ledger refused it.
- Questions answered through `report`, including one requiring the
  relevant line to be extracted from a larger file.
- A CSV edited correctly with no CSV-specific code anywhere: "mark every
  pending Software expense as approved" changed exactly the two matching
  rows and left the pending Office row alone.
- Run history: older runs reopen from the checkpoint database.
- Ledger panel reads the real database rather than reporting what the
  agent claimed.

### Known gaps

- **No stop button during a step.** Execution runs inside the Streamlit
  request, so there is nothing to cancel against mid-call. Closing the
  page is safe -- the run is checkpointed and resumable -- but a real
  cancel needs the graph on a background thread.
- **The sandbox is the only reachable directory.** Deliberate, but it
  means real files must be copied in. A nominated second root is the
  natural next step.
- **Prompt rules are followed inconsistently.** Rule 3b (do not copy
  values from the task) is obeyed most of the time; one run still filed
  `vendor: "Acme"` where the document said `ACME CORPORATION`. A prompt
  is a request, not a gate -- the same lesson as the ISO date, where the
  fix was enforcing it in the tool. The UI partly mitigates this: the
  wrong value is visible in the plan table and editable before approval.
- **No LLM response cache**, so development still burns daily quota and
  failures do not reproduce deterministically.

### Next

**Phase 4 -- memory.** Episodic storage of past runs, retrieval into the
planner prompt, and reflection that turns failures into lessons. The
`torch` install problem resurfaces here; the fallback is SQLite plus
numpy cosine similarity behind the same interface.

---

## Phase 4 -- Memory

**Date:** 2026-10-11
**Goal:** The agent remembers finished runs and plans better next time.
**Status:** Working, with a measured before/after.

### Files

| File | Purpose |
|---|---|
| `memory/store.py` | SQLite: episodes, lessons, facts. |
| `memory/semantic.py` | Embeddings and similarity, behind one interface. |
| `core/reflector.py` | Turns a finished run into memories. |
| `core/planner.py` | `recall()` -- puts memory into the prompt. |
| `core/graph.py` | A `reflect` node after `finalise`. |
| `app.py` | Memory panel: every lesson visible and deletable. |

### Design decisions

| Decision | Choice | Reasoning |
|---|---|---|
| What memory is | Retrieval, not training | Look up similar past runs, inject their lessons. Anticlimactic, and what production agents actually do. |
| Retrieval cutoff | A similarity threshold, not just top-k | Without it the three *least unrelated* episodes always come back, so a new kind of task arrives with three misleading examples -- and the planner tries to follow them. "Book a flight to Tokyo" correctly returns nothing. |
| Embedder | Pluggable; real model if installed, hashed bag-of-words otherwise | Nothing above `semantic.py` knows which is running. The fallback never fails to install and keeps the suite offline. Measured difference on paraphrases: 0.22 vs 0.71. |
| Duplicate lessons | Refused by a UNIQUE constraint | Running the same failing task three times would otherwise put three copies of the same advice in front of the planner. |
| Stale lessons | Superseded, not deleted | The old row is kept pointing at its replacement. "What did it used to believe, and when did that change?" is worth being able to answer. |
| Facts vs lessons | Facts overwrite; lessons supersede | A vendor's address changing is not an interesting history. A belief about how to work changing is. |
| Reflection failing | Never fails the run | The work is already done and verified. Memory is an enhancement. |
| Memory review | A UI panel with delete buttons | See problem 32. |

### Problems encountered

28. **Repairs were invisible to reflection.** `repair_node` dropped the
    failed result so the final results list reflected what actually
    happened -- which also deleted the most instructive event in the run.
    Reflection saw only an attempts counter and produced platitudes:
    *"verify the file exists first"*, which is already in the planner's
    instructions. **Fix:** keep a repair log (what was tried, why it
    failed, what replaced it) and pass it to the reflector. Lessons
    immediately changed to *"Convert all extracted dates to ISO format
    before calling add_invoice"* -- a real constraint of this system.

29. **Memory reached the wrong component.** It was injected only into the
    planner prompt. But *"convert dates to ISO"* is not advice about
    which steps to run -- it is advice about what to put in an argument,
    and that decision is made by the resolver. The planner read the
    lesson, wrote a correct plan, and the resolver produced `02/10/2026`
    anyway. **Fix:** the resolver sees lessons too.
    **Lesson:** memory has to reach the component that makes the
    decision, not the one that looks like it should know.

30. **The goal judge saw planned arguments, not resolved ones.** It was
    shown `{{step_2.output}}` placeholders and concluded that raw text
    had been dumped into every field, failing a correct run. **Fix:**
    `StepResult` now carries the arguments the tool was really called
    with. **This was the fifth instance of one pattern** -- see the note
    below.

31. **Installing a better embedder stranded the old memories.** Vectors
    from different models are not comparable: different dimensions,
    different geometry. Mixing them means retrieval silently returns
    nothing while appearing to work. **Fix:** store which embedder made
    each vector, filter by the current one, and `reembed_all()` to
    migrate. Then a second bug underneath it -- `CREATE TABLE IF NOT
    EXISTS` does nothing to a table that already exists, so the new
    column never reached the existing database. It worked on a fresh
    machine and failed on this one. **Fix:** an explicit migration list
    applied on connect.

32. **Memory amplifies the system's own bugs.** The first reflection
    produced *"Ensure the final step performs the actual filing action"*
    -- learned from a run that a then-buggy goal verifier had wrongly
    failed. It also recorded *"Wayne Enterprises: invoice_location =
    invoices/"* for a vendor whose invoice does not exist.

    The bugs were fixed; the lessons would have outlived them, injected
    into every future plan as though they were facts about the world. A
    bad memory does not throw an error.

    **Fix:** the Memory panel shows every lesson with a delete button and
    marks the ones learned from failed runs. Everywhere else this project
    refuses to take the model's word for anything; memory is the one
    place where a wrong belief is invisible, so it gets the same
    treatment.

### The pattern that has now appeared five times

Problem 9 (the step judge could not see the earlier output), 16 (the
probe did not know which file the step touched), 24 (the goal judge did
not know which tools ran), 30 (the goal judge saw placeholders), and the
original confabulation in Phase 1.

Every time: **a model asked to judge, without being shown what actually
happened, invented a confident answer rather than saying it could not
tell.** Every time the instinct was to reword the prompt, and every time
the fix was the inputs.

### Measured

The Umbrella invoice needed a repair before memory and none after: the
ISO-date lesson reached the resolver and the date was right first try.
That is the whole claim of this phase, demonstrated on one task.

---

## Phase 5 -- Capability breadth

**Date:** 2026-10-11 to 2026-10-12
**Goal:** Stop being a file-and-invoice tool. An agent that only touches
`sandbox/` is the spine of an AI employee, not one.
**Status:** 24 tools. Office, email, browser and full database editing.

### What was added

| Area | Tools |
|---|---|
| Files | multi-workspace: `sandbox`, `outputs`, plus any folder named in `.env` |
| Office | `excel_read` `excel_update` `word_read` `word_write` -- via COM |
| Email | `search_inbox` `save_draft` `send_email` `find_sent` -- SMTP/IMAP |
| Browser | `browser_open` `browser_read` `browser_type` `browser_click` `browser_click_confirmed` `gmail_compose` |
| Database | `update_invoice` `delete_invoice` added to the existing three |
| Verification | `tools/source_check.py` |

### Design decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Office automation | COM, not a headless library | The usual trade-off is visible-but-unreliable (clicking pixels) or reliable-but-invisible (writing the file). COM is both: Excel genuinely opens and the user watches, while every instruction is an API call. The demo requirement was to *see* the work happen. |
| Preference order | API > COM > accessibility tree > pixels | Reliability falls roughly 99% / 99% / 85% / 65% per step. The fallbacks exist so the agent is not helpless with an unanticipated app; they are not the first choice. |
| Email transport | SMTP/IMAP as well as the browser | A send can be *verified* by reading the Sent folder. A browser click leaves nothing to check but a screenshot. |
| Browser profile | A dedicated one, not the user's Chrome | Chrome locks its own profile, and pointing an agent at the browser holding every live session is a far larger blast radius than this needs. The user signs in once by hand; the agent inherits the session and never handles the password. |
| Generic click risk | The tool inspects its target | See problem 34. |
| New files | An `outputs/` workspace | Generated documents were landing beside the fixtures they came from, so after a few runs there was no telling the agent's work from its inputs. Editing an existing file still edits in place -- "everything to outputs" would silently copy and leave the original stale. |
| `invoice_number` | Not editable | It is the record's identity and what the UNIQUE constraint protects. "Changing" it is filing a different invoice. |
| Edits revalidate | Same rules as filing | Otherwise the edit path is a back door: refuse an ambiguous date on insert, accept it through `update_invoice`. |

### Problems encountered

33. **Two tool signatures that were wrong for a model to fill.**
    `excel_update` first took `changes_json`, which asked the model to
    produce JSON nested inside a JSON argument -- double-encoded, and
    wrong often enough to fail the task. Replaced with
    `"E2=approved; E3=rejected"`. The same shape as the earlier
    `arguments_json` bug.
    **Lesson:** a tool's signature is part of its prompt. An argument
    that is awkward to produce is a design flaw in the tool, and the fix
    belongs in the signature rather than in more instructions.

34. **A generic click breaks the risk model.** Every other tool has a
    fixed tier, which is what makes the approval gate meaningful.
    `browser_click` can click "Scroll down" or "Delete all mail" -- one
    function spanning every tier, which the gate cannot classify.
    **Fix:** the tool inspects its target, refuses anything matching
    send/delete/pay/submit/..., and directs it to
    `browser_click_confirmed`, which is IRREVERSIBLE. Substring matching,
    because the two errors are not symmetric: a false positive costs one
    approval, a false negative means something irreversible happens
    unasked.

35. **A browser left running locked its own profile.** A process exiting
    while its Chromium was still alive left the persistent profile
    locked, and the next run died with an `EPIPE` from inside the
    Playwright driver -- an error that says nothing about the cause.
    **Fix:** an `atexit` handler. Also noted: a careless
    `taskkill /IM chrome.exe` would have killed the user's own browser;
    processes must be identified by executable path, not by name.

36. **The resolver was told never to summarise.** Its prompt said *"copy
    values verbatim; do not summarise, reformat or improve them"* --
    correct for an invoice number, and the reason a request for a
    one-paragraph summary produced a Word document containing a raw
    spreadsheet dump, and an email body containing the whole of
    notes.txt. **Fix:** the prompt now names two jobs and gives a test
    for telling them apart -- read/find/get/extract means copy;
    write/summarise/draft/compose means compose. Verified both
    directions, including no regression on invoice extraction.

37. **The wrong-date bug, finally fixed.** An invoice dated 10/07/2026
    "(US format)" was filed as 2026-07-10 -- July instead of October --
    and every check passed, because each asked whether a record existed
    rather than whether it was right.

    Three prompt rules were tried: postconditions must be checkable,
    must name the entity, must compare values. Each improved the wording
    of the postcondition. None made the judge compare, because a model
    asked "does this match?" about work it just did agrees with itself.

    **Fix:** `tools/source_check.py` -- deterministic corroboration in
    Python. It refuses a vendor, number or amount that does not appear in
    the document, and a date the document does not state. Ambiguous dates
    are refused rather than guessed, and the error names both readings
    and says what evidence to look for. Explicit format notes outrank
    geography, because the invoice says "(US format)" *and* is addressed
    in Manchester, and weighing those equally cancels them out.

    **Verified end to end:** the agent made the same wrong reading, the
    tool refused with an explanation, and the repair ladder filed
    2026-10-07 unaided.

    **Lesson, now proven three times** (ISO dates, click safety, this):
    a prompt is a request, a tool is a gate. And the second half, which
    took longer to see: *a refusal is only useful if its message explains
    the fix.* "Invalid date" would have left the agent stuck.

### Verified live

- Excel opened on screen, compared actuals against targets and wrote
  three verdicts correctly -- with nothing in the code knowing what
  "beat its target" means.
- A spreadsheet read in Excel became a prose summary in Word.
- A real email composed in Gmail through the browser and sent after
  review.
- A ledger row edited; a delete fired gate 2, abort left the row intact,
  approve removed it.
- Four traversal attempts still refused, now across multiple workspaces.

### Known gaps

- **No LLM response cache.** Development still burns the daily quota and
  failures do not reproduce, which has repeatedly made debugging harder
  than it needed to be.
- **No stop button mid-step.** Execution runs inside the Streamlit
  request; a real cancel needs the graph on a background thread.
- **Desktop control (pywinauto) is not built.** COM covers Office, but
  an application with neither an API nor COM cannot be driven at all.
- **Office documents stay open** after the agent uses them, which locks
  the files against other tools until something closes them.
- **No tests for the Office, browser or mail tools** -- they need the
  applications present, so they are outside the offline suite.

---

## Phase 6 -- A web interface of its own

**Date:** 2026-10-11 to 2026-10-12
**Goal:** Replace Streamlit with something that looks like a product.
**Status:** Done. Third front end on the same graph.

### Files

| File | Purpose |
|---|---|
| `server.py` | Starlette API; execution streamed as server-sent events. |
| `web/index.html`, `style.css`, `app.js` | The interface. No framework, no build step. |
| `tools/report.py` | How the agent answers a question. |

`core/` was not touched. Streamlit and the custom interface both drove
the same compiled graph, which is the dividend from keeping `app_cli.py`
free of agent logic in Phase 1.

### Design decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Web framework | Starlette, already present as a Streamlit dependency | Routing, JSON and a streaming response is the whole requirement; FastAPI would add nothing. |
| Execution transport | Server-sent events | A request that blocked until the agent finished would leave the page silent for a minute and then dump a result. Watching the work happen is most of the point. |
| Front end | Plain HTML, CSS and JS | A few hundred lines of DOM work. A build step would cost more than it saved. |
| Where gates come from | The checkpointed snapshot | So a refreshed browser, or a new session tomorrow, picks a paused run up where it stopped. |
| Colour | One accent, used for meaning | On a screen where work is approved before it runs, colour should carry meaning: amber is the agent's voice, green and red mean only whether a step passed. |
| Background motion | Transform and opacity only | Those the browser moves on the compositor without repainting. Animating a gradient's position repaints every frame, which on a page also streaming live updates is the difference between smooth and stuttering. |

### Problems encountered

38. **An icon with zero width.** The sidebar button rendered as an empty
    square: a shared `button { padding: 9px 20px }` applied to a fixed
    40px box leaves exactly nothing for the glyph. Only visible by
    measuring the element -- it looked like a styling choice.

39. **A wrong diagnosis, corrected by measuring.** Plans were failing
    with "Model output did not match Plan", and the raw output in the
    error appeared to stop mid-sentence, so the conclusion was
    truncation and `max_output_tokens` was added. Checking properly:
    `finish_reason` was `STOP` and the JSON was complete. **The "cut
    off" output was this project's own 1500-character slice in the error
    message** -- a misread of our own debug output. The real cause was an
    unquoted `{{step_N.output}}` in a numeric field, repaired
    mechanically in `Step`'s validator.

40. **The agent could not answer a question.** Asked to show the second
    entry of a CSV, it read the file and reported success with the whole
    file as output. Every tool changed or inspected the world; none
    could *say* anything, so a task could be executed perfectly and
    leave the person who asked no better off. **Fix:** `report(answer)`,
    which returns what it is given -- the value is that argument
    resolution fills it from earlier outputs guided by the step's
    intent. Adding a whole new category of capability cost one decorated
    function and one prompt rule.

41. **The composer never came back.** The task box was hidden when a run
    started and never restored, leaving the page a dead end after every
    task. It now returns as soon as a run stops waiting on the user, and
    stays hidden only while a gate is open.

---

## Phase 7 -- Reaching the rest of the machine

**Date:** 2026-10-12
**Goal:** Stop being limited to the applications we anticipated.
**Status:** 32 tools. Any Windows application, any website, and -- behind
a gate -- arbitrary code.

### Files

| File | Purpose |
|---|---|
| `tools/desktop.py` | Any Windows app, through the accessibility tree. |
| `tools/shell.py` | `run_python`, `run_command`. The last resort. |
| `tools/browser.py` | Playwright, on a thread of its own. |
| `tools/mail.py` | Gmail over IMAP/SMTP, registered only when configured. |
| `tools/source_check.py` | Corroborating recorded values against their document. |
| `tests/test_shell_guards.py` | 35 tests on the refusal list. |

### Design decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Preference order | API > COM > accessibility > pixels | Roughly 99 / 99 / 85 / 65 per cent per step. The fallbacks exist so the agent is not helpless with an unanticipated app; they are not the first choice, and the docstrings say so, because the catalogue is a prompt. |
| Generic click risk | The tool inspects its target | One click function cannot have one risk tier. Anything matching send/delete/pay/install is refused and routed to an IRREVERSIBLE variant. Substring matching, since a false positive costs one approval and a false negative does not. |
| Shell tools | Built, but always IRREVERSIBLE | An agent that cannot reach an unanticipated tool is not much of a colleague. The gate showing the exact code is what keeps that from being reckless. |
| The refusal list | Short, and described as a backstop | A long denylist invites the belief that anything not on it is safe. It guards against the unrecoverable; the real protection is a person reading the code at the gate. |
| Email sending | The agent never sends | It composes; the person sends. A message to a real recipient cannot be recalled, and approving a plan is not consent to send *that particular wording*, which is only visible at the end. |
| Outputs | A folder on the Desktop | Generated files were landing in the source tree, which is not where anyone looks for their own documents. |

### Problems encountered

42. **Playwright is bound to the thread that created it.** The server
    runs each request on a pool thread, so step 1 and step 3 of one plan
    arrived on different threads and the second failed with "cannot
    switch to a different thread" -- while the browser sat there visibly
    working. **Fix:** the browser owns one permanent thread and every
    tool sends it work through a queue, the standard shape for wrapping
    a thread-affine library.

43. **A killed browser wedges its own profile.** Chromium marks a
    profile in use with `SingletonLock` and removes it on a clean exit.
    A browser that was killed leaves it behind, and every later launch
    is refused for a profile nothing is using. **Fix:** clear stale lock
    files before launching. Related: `_startup_error` was never reset,
    so one failed start was permanent even after the cause went away.

44. **A careless kill.** Clearing a wedged browser with
    `taskkill /IM chrome.exe` would have killed the user's own Chrome --
    it shares the image name. It matched nothing by luck. Processes are
    now identified by executable path.

45. **Typing into someone's open document.** The first desktop test used
    `_find_window("Notepad")`, which matched the *first* window
    containing that word -- the user's open `requirements.txt`, not a
    blank one -- and replaced its contents. Undone immediately with
    Ctrl+Z; nothing reached disk. **Fix:** when several windows match,
    the tool refuses and lists them rather than picking one. Guessing
    which of two things someone meant is the wrong answer here for the
    same reason it is with an ambiguous date.

46. **Two error messages forming a loop.** A second Excel instance made
    `Workbooks.Open` return `None` rather than raise, so `excel_update`
    died on a bare `AttributeError`; repair switched to `write_file`,
    which failed on the file lock and suggested `excel_update`. Both
    messages were individually correct and together had no exit. **Fix:**
    the error names the second-instance problem and offers a third route
    -- writing to `outputs/` -- that works whatever Excel is doing.
    **Lesson:** error messages are part of the agent's control flow.
    When a tool suggests an alternative, check the alternative cannot
    bounce straight back.

47. **Unusable tools in the catalogue.** A browser-composed email
    verified correctly at every step, then the verifier chose
    `find_sent` as its probe, could not connect without IMAP
    credentials, and failed a step whose work had succeeded. The mail
    tools registered unconditionally. **Fix:** they register only when
    Gmail is configured. An unusable tool in the catalogue is not a
    missing feature, it is a false promise, and the verifier believes it.

48. **The resolver had only two jobs.** Its prompt distinguished
    extracting a value (copy verbatim) from composing prose -- and
    "write a copy of this CSV with a new column" is neither, so copying
    won and the file came back unchanged. **Fix:** transformation is a
    third job, with the trap stated outright, because most of the
    content genuinely *is* meant to be copied. The verb in the intent
    decides: read/get/extract copy, write/summarise/draft compose,
    add/update/mark/sort transform.

49. **The goal judge did not know what `outputs/` meant.** It read
    "saved to outputs/promotions.csv" and "save it to the Desktop
    folder" as two different places and failed a run that did exactly
    what was asked. **Fix:** it is told what the workspace aliases
    resolve to. The agent uses short names deliberately -- machine paths
    in a plan are unreadable -- so whatever judges the plan has to know
    them.

50. **The wrong-date bug, closed.** An invoice dated 10/07/2026 "(US
    format)" was filed as 2026-07-10 and every check passed, because
    each asked whether a record existed rather than whether it was
    right. Three prompt rules were tried first; each improved the
    wording of the postcondition and none made the judge compare.
    **Fix:** `tools/source_check.py` -- deterministic corroboration in
    Python. Ambiguous dates are refused rather than guessed, explicit
    format notes outrank geography, and the error names both readings.
    Verified end to end: the agent made the same wrong reading, the tool
    refused with an explanation, and the repair ladder filed 2026-10-07
    unaided.

### Where the project stands

- 32 tools, 7,800 lines, 192 tests, all offline.
- Six capability areas: files, documents, spreadsheets, database, web,
  any Windows application -- plus a gated shell.
- Five tools stop for human approval: `run_python`, `run_command`,
  `delete_invoice`, `browser_click_confirmed`, `click_control_confirmed`.

### Known gaps

- **"The latest invoice from a vendor"** -- the original problem
  statement says *latest*; selection is by vendor, not by date.
- **No cancel mid-step.** The run is checkpointed, so closing the page is
  safe, but a running step finishes.
- **No LLM response cache.** Development still burns the daily quota and
  failures do not reproduce, which made several bugs here harder to find
  than they needed to be.
- **No tests for the Office, browser, desktop or mail tools** -- they
  need the applications present, so they sit outside the offline suite.
- **`run_python` / `run_command` are a hole in every other guarantee.**
  The agent reads documents it did not write, and every other tool is
  safe against an injection because the tool is narrow. Here the only
  thing between a malicious invoice and the machine is a person reading
  the gate.

### The pattern worth taking away

Counting problem 49, a model judging without being shown what actually
happened invented a confident answer **six times**: the step judge
without the earlier output, the probe without the file it had touched,
the goal judge without the tools that ran, again without the resolved
arguments, again without the workspace aliases, and the original
confabulation in Phase 1.

Every time the instinct was to reword the prompt. Every time the fix was
the inputs.

The companion lesson, proven four times -- the ISO date, the click
guard, the recipient check, the source corroboration:

> A prompt is a request. A tool is a gate. Where correctness matters,
> enforce it in code -- and make the refusal explain the fix, or the
> agent cannot act on it.
