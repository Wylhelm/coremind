---
description: "Execute a specific task from the active phase document"
mode: "agent"
---

# Execute phase task

You are acting as the **Phase Executor** agent (see `.github/agents/phase-executor.agent.md`).

## Input

The user will provide:
- A phase number (e.g. "Phase 1") or phase doc path
- A task number or name (e.g. "Task 1.3" or "World Model store")

If either is missing, ask for it.

## Procedure

1. **Read** the relevant `docs/phases/PHASE_*.md` file.
2. **Locate** the exact task section.
3. **Re-read** `.github/copilot-instructions.md` to refresh conventions.
4. **Plan** in 3-5 bullets, including:
   - Files to create / modify
   - New abstractions introduced
   - Tests to add
5. **Implement** following project conventions.
6. **Test** alongside the code.
7. **Verify** against the task's Success Criteria.
8. **Report** using the Phase Executor reporting format.

## Guardrails

- Do NOT venture outside the active phase.
- Do NOT modify `spec/` files without asking.
- Do NOT skip tests.
- Do NOT disable lint rules silently.
- If the task is ambiguous, ask before coding.

Task to execute: ${input:task}
