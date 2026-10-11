"# Autonomous_AI-Employee" 

                        ┌─────────────────────────────┐
   user types task ───► │  1. INTENT / CLARIFIER      │
                        │  resolve ambiguity, ask Qs  │
                        └──────────┬──────────────────┘
                                   │
                   ┌───────────────▼───────────────┐
    MEMORY ───────►│  2. PLANNER (LLM)             │
  (similar past    │  reads: task + tool catalog   │
   runs, lessons,  │         + memory + env facts  │
   entity facts)   │  emits: typed Plan (steps,    │
                   │   tool, args, postcondition,  │
                   │   risk tier)                  │
                   └───────────────┬───────────────┘
                                   │
                   ┌───────────────▼───────────────┐
                   │  3. HUMAN GATE #1             │◄── accept / reject /
                   │  plan shown in UI             │    edit steps / re-plan
                   └───────────────┬───────────────┘
                                   │ approved
          ┌────────────────────────▼────────────────────────┐
          │  4. EXECUTOR LOOP   (per step)                  │
          │                                                 │
          │   a. is step risk == HIGH?  ──► HUMAN GATE #2   │◄── approve payload
          │   b. invoke tool via REGISTRY                   │
          │   c. OBSERVE (return value / a11y tree / shot)  │
          │   d. VERIFY postcondition                       │
          │   e. pass? ─► next step                         │
          │      fail? ─► REFLECT (LLM) ─► retry / repair   │
          │                plan / escalate to human         │
          │                                                 │
          │   every event ─► EVENT BUS ─► live UI + JSONL   │
          └────────────────────────┬────────────────────────┘
                                   │
                   ┌───────────────▼───────────────┐
                   │  5. FINAL VERIFIER            │
                   │  task-level goal check        │
                   └───────────────┬───────────────┘
                                   │
                   ┌───────────────▼───────────────┐
                   │  6. REFLECTOR ─► writes to    │
                   │  MEMORY (episode + lessons)   │
                   └───────────────────────────────┘