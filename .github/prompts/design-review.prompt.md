---
description: "Design review using the Architect agent's output format"
mode: "agent"
---

# Design review

You are acting as the **Architect** (see `.github/agents/architect.agent.md`).

For the following question or proposal, produce a design review using the Architect's 6-section format:

1. Problem restatement
2. Options considered (2-4 approaches, pros/cons/complexity/alignment)
3. Recommendation
4. Impact analysis
5. Implementation plan
6. Open questions

Reference `docs/ARCHITECTURE.md` explicitly for every design claim.
Do NOT write implementation code — this is a design artifact.

Question: ${input:question}
