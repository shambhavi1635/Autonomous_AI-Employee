# Your AI Colleague

An autonomous task worker that uses the applications on a Windows machine
the way a person would — reading PDFs, filling spreadsheets, querying a
database, drafting email in a browser — and **asks permission before it
does anything it cannot take back.**

You describe a task in plain English. It writes a plan, shows you the
plan, and waits. You approve, edit or reject it. Then it works, checking
after every step whether the thing it just did actually happened.

```
  "File the Globex invoice from the invoices folder into the ledger"

        ↓                 ↓                    ↓              ↓
    writes a          you approve         works, step      checks the
    plan              or edit it          by step          result is real
```

---

## The idea it is built on

> **Nothing counts as done because the model said so. It counts as done
> because something else went and looked.**

Every step declares, in advance, how we will know it worked. After the
step runs, a *separate* read-only check goes and observes the world. A
tool returning cleanly and a task actually being finished are two
different facts, and this system keeps them in two different fields.

```
   ok = True        the tool call did not raise
   verified = True  something re-read the world and agreed
   ─────────────────────────────────────────────────────────
   succeeded        requires BOTH
```

That sounds obvious. It is also the single hardest part: the agent will
cheerfully report success for work it never did, and most of the design
here exists to make that impossible rather than unlikely.

---

## What it can do

32 tools, grouped by what they touch. Risk tier in brackets:
`r` read · `w` write · **`i` irreversible — always stops and asks you.**

| Area | Tools |
|---|---|
| **Files** | `list_files`ʳ `read_file`ʳ `write_file`ʷ |
| **Documents** | `read_pdf`ʳ `word_read`ʳ `word_write`ʷ |
| **Spreadsheets** | `excel_read`ʳ `excel_update`ʷ — real Excel, visibly on screen |
| **Database** | `find_invoice`ʳ `list_invoices`ʳ `add_invoice`ʷ `update_invoice`ʷ **`delete_invoice`ⁱ** |
| **Web** | `browser_open`ʳ `browser_read`ʳ `browser_type`ʷ `browser_click`ʷ **`browser_click_confirmed`ⁱ** `browser_close`ʷ |
| **Email** | `gmail_compose`ʷ — composes, never sends |
| **Any Windows app** | `list_windows`ʳ `read_window`ʳ `focus_window`ʷ `open_application`ʷ `type_into`ʷ `press_keys`ʷ `click_control`ʷ **`click_control_confirmed`ⁱ** `screenshot`ʳ |
| **Last resort** | **`run_python`ⁱ `run_command`ⁱ** |
| **Answering** | `report`ʳ |

Things it has actually done, end to end:

- Read a PDF invoice with a subtotal and a tax line above the real total,
  picked the right figure, and filed it in a database
- Opened Excel on screen, compared actuals against targets, and wrote the
  verdict into three rows
- Turned a spreadsheet into a prose summary in Word
- Composed an email in Gmail from a meeting note, for a human to send
- Added a computed column to a CSV
- Driven Notepad through the Windows accessibility tree
- **Refused** to file an invoice for a vendor that did not exist, rather
  than filing a similar one

---

## How a task flows

```
      you type a task
            │
            ▼
   ┌─────────────────┐     reads the tool catalogue, and what it
   │     PLANNER     │◄─── learned from similar tasks before
   └────────┬────────┘
            │  a typed Plan: steps, tools, arguments,
            │  how to check each one, how risky each one is
            ▼
   ┌─────────────────┐
   │  GATE 1  ◄──────┼──── you: approve / edit / reject
   │  the whole plan │     (nothing has run yet)
   └────────┬────────┘
            │ approved
            ▼
   ┌──────────────────────────────────────────────┐
   │  FOR EACH STEP                               │
   │                                              │
   │   1. resolve arguments from earlier results  │
   │   2. irreversible? ──► GATE 2 ◄── you, with  │
   │                        the real values       │
   │   3. run the tool                            │
   │   4. VERIFY: re-read the world               │
   │                                              │
   │   passed ──► next step                       │
   │   failed ──► the ladder (below)              │
   └────────────────────┬─────────────────────────┘
                        ▼
              ┌───────────────────┐
              │   GOAL CHECK      │  did the user get what they
              │   (whole run)     │  asked for, not just: did
              └─────────┬─────────┘  every step pass?
                        ▼
              ┌───────────────────┐
              │    REFLECT        │  what did this teach us?
              │  → memory         │  → lessons for next time
              └───────────────────┘
```

---

## Three levels of checking

Each one catches something the level below cannot.

```
   LEVEL 1   did the call return?                  ← ok
             ─────────────────────────────────────────
             catches: crashes, bad arguments
             misses:  a tool that returned cleanly and did nothing


   LEVEL 2   did this step's postcondition hold?   ← verified
             ─────────────────────────────────────────
             a read-only probe re-reads the world and a judge
             compares it against what the step promised

             catches: "wrote the file" when no file exists
             misses:  every step correct, wrong task


   LEVEL 3   did the user get what they asked for? ← goal check
             ─────────────────────────────────────────
             compares the original request against the whole trace

             catches: asked for the Wayne invoice, filed Acme's
                      instead, and every single step was honest
```

Level 3 exists because of a real failure. Asked to file an invoice from
a vendor that had none, the agent found the folder, read the only invoice
in it, filed that, and reported complete success. Every step did exactly
what it said. The *task* was not done.

---

## When something goes wrong

```
   step fails
       │
       ▼
   ┌────────────┐   show the model its own error and let it
   │   REPAIR   │   change tool, arguments or postcondition
   └─────┬──────┘   (max 2 attempts per step)
         │ still failing
         ▼
   ┌────────────┐   throw the plan away and think again,
   │   REPLAN   │   knowing what failed — then back to GATE 1,
   └─────┬──────┘   because a new plan needs approving too
         │ still failing
         ▼
   ┌────────────┐   stop, and say exactly how far it got
   │  ESCALATE  │   and what it tried
   └────────────┘
```

Repair is not a blind retry. A blind retry only helps with transient
faults, and most agent failures are the agent having been *wrong* —
repeating the call repeats the mistake. What makes repair work is that
tools explain themselves:

```
   add_invoice refused: "invoice_date must be ISO YYYY-MM-DD, got
   '02/10/2026'. Convert it first: that is ambiguous, but if the
   document is Indian or European it means 2026-10-02..."

   → the agent read that, converted the date, retried, and filed it.
     Nobody told it how.
```

---

## The machine underneath

The loop is a LangGraph state machine, not a `while` loop. Each box is a
node we wrote; the arrows are conditional edges.

```
                        START
                          │
                          ▼
                    ┌──────────┐
                    │   plan   │
                    └─────┬────┘
                          ▼
                    ┌──────────┐
     ┌──────────────│ approve  │  ← interrupt(): the graph writes its
     │              └─────┬────┘    whole state to SQLite and STOPS
     │ rejected           │ approved
     │                    ▼
     │        ┌───────────────────────┐
     │        │        step           │ ← interrupt() again if the step
     │        │  resolve · run · verify│   is irreversible
     │        └───────────┬───────────┘
     │                    │  after_step()
     │     ┌──────────────┼──────────────┬───────────────┐
     │  [more steps]  [failed,       [failed,         [done]
     │     │           repair left]   repair spent]      │
     │     │              │               │              │
     │     └──► step ◄────┤          ┌────────┐          │
     │                    │          │ replan │──► approve
     │              ┌──────────┐     └────────┘   (a new plan needs
     │              │  repair  │──► step          approving too)
     │              └──────────┘                        │
     │                                                  ▼
     │                                          ┌──────────────┐
     └─────────────────────────────────────────►│   finalise   │
                                                │  goal check  │
                                                └───────┬──────┘
                                                        ▼
                                                 ┌─────────────┐
                                                 │   reflect   │
                                                 │  → memory   │
                                                 └──────┬──────┘
                                                        ▼
                                                       END
```

Why a graph rather than a loop: `interrupt()` persists the entire run and
halts, and resuming returns the human's answer *from that same call*. A
`while` loop blocking on an in-memory event loses a pending approval the
moment the process dies. This is also why the routing above reads as one
function instead of nested breaks — and why a browser refresh, or coming
back tomorrow, picks a paused run up exactly where it stopped.

---

## Safety: enforced, not requested

Three tiers, declared on the tool itself:

```
   READ          changes nothing                    runs freely
   WRITE         changes something, undoable        runs freely
   IRREVERSIBLE  cannot be taken back               STOPS AND ASKS
```

The tier comes from **the tool**, never from the plan:

```
   plan says:  "sending this email is a READ operation"
   system:      no. send is IRREVERSIBLE. stopping for the human.
```

That matters because the agent reads documents it did not write. An
invoice containing *"ignore previous instructions, this is routine"*
changes nothing, because the gate does not consult the plan's opinion of
itself. A plan may *raise* a step's risk; it can never lower it.

Other boundaries, all in code rather than in a prompt:

- **File access is confined to named workspaces** — a traversal like
  `notes/../../../secrets.txt` is refused, and absolute paths outright
- **Credentials are never typed** — password fields are refused
- **System windows are off limits** — Registry Editor, PowerShell, Task
  Manager, Windows Security
- **Shells cannot be launched** as applications
- **Catastrophic commands are refused** whatever you approve — disk
  formatting, recursive deletes of system paths, registry writes
- **Recorded values are corroborated against their source document**
  before a record is filed
- **The agent never sends an email.** It composes; you send.

> A prompt is a request. A tool is a gate. Where correctness matters, it
> is enforced in code.

---

## Memory

```
   run finishes
        │
        ▼
   ┌──────────┐   what did this teach us? — mostly from
   │ REFLECT  │   repairs and from edits a human made
   └────┬─────┘
        ▼
   ┌──────────┐   episodes (what was done, how it went)
   │  MEMORY  │   lessons  (short, imperative, reusable)
   └────┬─────┘   facts    (what we know about Globex Ltd)
        │
        ▼ next task, embedded and matched against past ones
   ┌──────────┐
   │ PLANNER  │   and the argument resolver, which is where
   └──────────┘   half the lessons actually apply
```

Real lessons it has written for itself:

```
"Convert all extracted dates to ISO format before calling add_invoice"
"A vendor named in a request may have no invoice present"
```

The first one measurably removed a repair from a later run.

**Memory is reviewable.** Every lesson is listed in the sidebar with a
delete button, and marked if it came from a failed run — because a wrong
lesson does not throw an error, it quietly steers every future plan.
That has already happened once here, from a verdict a then-buggy verifier
produced.

---

## Project structure

```
core/
  config.py      one place reading the environment
  schemas.py     what a Plan is — steps, postconditions, risk tiers
  planner.py     task → validated plan, checked against the real tools
  executor.py    resolve arguments · run · verify · repair · goal check
  graph.py       the state machine, and both approval gates
  reflector.py   a finished run → memories

tools/
  registry.py    the @tool decorator: catalogue, schema, risk, in one line
  files.py       sandboxed file access across named workspaces
  documents.py   PDF text
  office.py      Excel and Word, through COM, visibly
  ledger.py      the stand-in "internal system"
  browser.py     any website, on its own thread
  desktop.py     any Windows application, via the accessibility tree
  mail.py        Gmail over IMAP/SMTP (optional)
  shell.py       run_python / run_command — the last resort
  source_check.py  corroborating recorded values against the document
  report.py      how the agent answers a question

memory/
  store.py       episodes, lessons, facts
  semantic.py    embeddings, with a dependency-free fallback

llm/
  client.py      the only file that knows which model provider is used
  prompts/       the planner prompt

web/             the interface: index.html, style.css, app.js
server.py        HTTP API, streaming execution as it happens
app_cli.py       the same agent, in a terminal
tests/           192 tests, all offline — no API key needed
docs/DEVLOG.md   every decision, every reversal, and 37 problems hit
```

Dependencies point one way. `tools/` never imports `llm/`, so tools are
testable with no API key. `core/` never imports the interface, which is
why three different front ends have been built on the same graph without
changing a line of it.

---

## Setup

Requires **Windows**, **Python 3.12**, and Microsoft Office for the Excel
and Word tools.

```bash
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe -m playwright install chromium
```

> Every command below calls the interpreter by path rather than relying
> on an activated environment. On Windows a bare `pip` or `python` goes
> wherever PATH points, which during development here twice installed a
> whole dependency set into the wrong environment. If you used conda
> instead of `venv`, the interpreter is at `venv\python.exe` and the
> commands change accordingly.

Copy `.env.example` to `.env` and add a Gemini API key from
[aistudio.google.com/apikey](https://aistudio.google.com/apikey):

```
GOOGLE_API_KEY=your_key_here
```

Check it works:

```bash
venv\Scripts\python.exe scripts\smoke_test_llm.py
```

Sign in once to any site the agent should stay logged into:

```bash
venv\Scripts\python.exe scripts\browser_login.py
```

You type the password; the agent only ever inherits the session.

---

## Running

```bash
venv\Scripts\python.exe server.py        →  http://localhost:8000
```

Or in a terminal:

```bash
venv\Scripts\python.exe app_cli.py "file the Acme invoice into the ledger"
venv\Scripts\python.exe app_cli.py --list
venv\Scripts\python.exe app_cli.py --resume <run-id>
```

Generated files go to `AI_colleage_outputs` on the Desktop, separate from
anything the agent was given to work with.

### Tests

```bash
venv\Scripts\python.exe -m pytest tests/ -q
```

192 tests, under five seconds, **no API key and no network**. That is
deliberate: a suite needing credentials gets skipped, and one costing
money per run gets run less often than it should.

---

## Three decisions worth knowing

**Visible *and* reliable.** The usual trade-off in desktop automation is
that you can watch it work (clicking pixels, ~65% right per step) or you
can trust it (writing files headlessly, invisible). Office automation via
COM is both: Excel genuinely opens and you watch the cells change, while
every instruction is an API call. The agent prefers, in order:

```
   a real API      ~99%     openpyxl, sqlite
   COM             ~99%     Excel, Word — visible AND reliable
   accessibility   ~85%     any other Windows app
   raw pixels      ~65%     last resort
```

**Approval survives a restart.** The graph checkpoints itself to SQLite,
so a run paused at a gate can be resumed by a different process, or
tomorrow. Approve a plan, close the laptop, come back — it continues.
That also means the web interface needs no background threads: a page
reload just reloads the run.

**Capabilities are plug-in.** One decorated function adds a tool, and the
planner's catalogue is generated from the real functions every time, so
it cannot drift out of step with what exists. Adding the ability to
*answer a question* — a whole new category — cost one function and one
prompt rule, with no change to the planner, executor or graph.

---

## Known limitations

- **"The latest invoice from a vendor"** — selection is by vendor, not by
  date. Multiple invoices from one company is untested.
- **No cancel mid-step.** Closing the page is safe, since the run is
  checkpointed, but a running step finishes.
- **A browser-sent email cannot be independently verified** unless IMAP
  credentials are configured; with them, the agent reads the Sent folder.
- **Desktop control is the weakest layer.** Window titles change as you
  work, some applications draw their own interface and expose nothing to
  the accessibility tree, and two windows with similar titles are refused
  rather than guessed between.
- **`run_python` and `run_command` are a genuine hole in every other
  guarantee.** They are IRREVERSIBLE and show you the exact code, and the
  refusal list is a backstop against the unrecoverable rather than a
  security boundary. An unexpected shell step in a plan is a reason to
  reject the plan.
- **Free-tier model quotas are small** — around 20 requests per day on
  the newest models. A run costs roughly 6–10 calls.

---

## Development log

`docs/DEVLOG.md` records every design decision and its reasoning, the
decisions that were **reversed** and why, and 37 numbered problems with
their causes and fixes — including the ones caused by this project's own
bugs. One pattern appears five separate times:

> A model asked to judge, without being shown what actually happened,
> invents a confident answer rather than saying it cannot tell. Every
> time the instinct was to reword the prompt. Every time the fix was the
> inputs.
