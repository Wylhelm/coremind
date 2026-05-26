# CoreMind v2 — Executive Summary

**Version:** 1.0.0 (Cognition)
**Date:** May 2026
**Status:** Design phase — building toward a self-improving intelligence

---

## 1. Where We Are

CoreMind v1 is not a prototype. It is a working, production-grade cognitive daemon that has been running continuously in a real home since April 2026. It observes, reasons, forms intentions, and acts — all without waiting for a human to tell it what to do.

The system is built on a **7-layer cognitive architecture** inspired by biological cognition: perception, a world model, episodic and semantic memory, reasoning, intention formation, graduated action execution, and reflection. Ten plugins feed its world model with real-time data: Home Assistant sensors and effectors, Gmail and Calendar via GOG, Apple Health metrics, financial transactions from Firefly III, task management from Vikunja, weather, system telemetry, Tapo cameras, webcams, and a bidirectional adapter to OpenClaw for conversational channels. The codebase sits at 86 Python modules backed by 46 test files, with a CI pipeline that enforces linting, type-checking, and test coverage on every push.

It works. But working is not the same as *getting better*.

---

## 2. The Sparks That Lit v2

Two events in early 2026 reshaped the conversation about what autonomous AI should look like. They came from opposite directions — one from the architecture of intelligence, the other from the practice of building it — but they converged on the same insight.

### 2.1 Yann LeCun and the World Model Revolution

In March 2026, Yann LeCun's startup **AMI Labs** (Advanced Machine Intelligence) raised over $1 billion at a $4.5 billion valuation. The thesis was not another LLM scaling run. LeCun's bet is on **JEPA** — Joint Embedding Predictive Architectures — and the idea that real intelligence requires a world model: an internal representation that learns from raw sensor data, predicts outcomes, and supports planning without needing to reconstruct every noisy pixel.

The philosophy behind JEPA is that intelligence is fundamentally about compression and prediction. An intelligent system abstracts away irrelevant detail and learns the latent structure of its environment. It builds a *model of the world* in embedding space, not in raw observation space. When it predicts what happens next, it predicts in this compressed, meaningful space — not in the space of raw sensor readings.

LeCun's framing is explicit: intelligence requires systems that **understand the world, have persistent memory, can reason and plan, and are controllable and safe**. These are not just nice-to-haves. They are the definition.

This is, by remarkable coincidence, the exact architecture CoreMind v1 already had. The 7-layer stack maps almost perfectly onto LeCun's desiderata: world model (L2), persistent memory (L3), reasoning (L4), planning (L5), controllability and safety (L6). CoreMind v1 anticipated the architecture that the frontier is now betting billions on.

But CoreMind v1's world model — while functional — is still a relational graph queried in discrete snapshots. It doesn't learn embeddings, it doesn't predict, it doesn't compress. The architecture is right, but the implementation is frozen. v2 brings the JEPA insight home: **teach the world model to learn representations, not just store facts.**

### 2.2 Andrej Karpathy and the Autonomous Loop

Around the same time, Andrej Karpathy released **autoresearch** — an AI agent that, left running for two days, conducted 700 experiments on a small language model training pipeline, discovered 20 optimizations, and produced an 11% speedup when applied to a larger model. Shopify's CEO Tobias Lütke replicated it overnight on internal data: 37 experiments, 19% performance gain.

Karpathy's key insight was not about training efficiency. It was about **autonomy as a gradient, not a switch**. His system didn't need to be perfectly autonomous to produce extraordinary results. It needed just enough autonomy to run experiments, evaluate outcomes, and iterate — with a human optionally contributing on the edges.

He described the emerging paradigm as a transition: from specification ("write exactly what the system should do") to **verification** ("define what success looks like, let the system find the path"). The role of the human shifts from programmer to reviewer, from author to auditor. Karpathy called this the start of **"the loopy era"** — an age where AI systems don't just answer questions, but run continuous improvement loops.

His follow-up vision was even bolder: "The next step for autoresearch is that it has to be asynchronously massively collaborative for agents. The goal is not to emulate a single PhD student, it's to emulate a research community of them."

For CoreMind, this is the missing piece. v1 has a reflection layer (L7) that evaluates its own performance weekly. But reflection in v1 is retrospective and passive: it looks back and says "was I useful this week?" — it doesn't look forward and say "how do I change my behavior to be more useful?"

---

## 3. What's Missing in v1

CoreMind v1 is a cognitive daemon, not a learning system. The difference matters. Here are the five gaps that v2 must close:

### 3.1 No Self-Improvement

The system runs the same code, the same prompts, the same thresholds every cycle. If it generates a bad intent — an irrelevant question, an unhelpful notification — it has no mechanism to learn from that failure. The only way it improves is when a human (Guillaume) edits a config file or tweaks a prompt by hand.

This is fundamentally different from how biological cognition works. A human who repeatedly asks irrelevant questions *stops asking those questions*. A human who notices that certain patterns consistently lead to useful insights *pays more attention to those patterns*. v1 does neither.

### 3.2 Binary Agency

v1's graduated consent protocol (safe / suggest / ask) is a good foundation. But it operates at the level of individual action classes — "all Home Assistant actions are safe" or "all financial actions require approval." There's no per-domain slider, no notion of "I trust you with lights but I want to see your climate recommendations before you act," and no way for the system to *earn* higher autonomy over time by demonstrating reliable judgment.

### 3.3 Verbose Token Usage

Every intention cycle in v1 feeds a full text snapshot of the world state — entity names, attribute values, recent events — into the LLM as raw text. For a home with dozens of entities, this means thousands of tokens burned on structured data that the LLM has to re-parse every cycle. Karpathy's world of 700 experiments in two days is only possible because each experiment costs a few minutes of GPU time. CoreMind's cycles cost tokens, and tokens cost latency and money. v1 is wasteful.

### 3.4 Stale Investigations

v1 has a mechanism for detecting when an investigation (an open question or hypothesis it's tracking) has gone stale — and it prunes it. But pruning is just cleanup. It doesn't close the loop. The system says "I guess that wasn't important" without asking *why* it wasn't important, without testing whether the hypothesis was wrong, and without incorporating the answer into its model of the world.

### 3.5 Fixed Parameters

Every threshold in v1 — salience minimums, confidence minimums, interval durations, quiet hours — is a hardcoded constant or a config file entry. These are parameters that a learning system should tune itself. The right salience threshold for a weekday morning is not the same as for a Sunday evening, but v1 treats them identically. The system that knows the most about the user's rhythms is the one least capable of adjusting to them.

---

## 4. What v2 Adds — Five New Capabilities

v2 is not a rewrite. It is an **upgrade** to an architecture that already works. Every v2 capability slots into the existing 7-layer stack, enhancing specific layers without breaking their contracts. The system continues to run, continues to observe, continues to act — but it now learns.

### 4.1 The Autonomy Slider

Instead of a binary trust model, v2 introduces **per-domain graduated autonomy**. Each domain (home automation, notifications, financial actions, health recommendations, communication) gets an independent autonomy level — from level 0 (fully manual, ask for everything) to level 5 (fully autonomous, trust the system's judgment).

The slider is not just a user preference. It is a **teachable parameter**. As the system demonstrates reliable judgment in a domain, it can recommend moving up a level. As it makes mistakes, it can recommend moving down. The user retains final authority, but the conversation shifts from "allow or deny every individual action" to "calibrate the system's domain-level trust."

This is the autonomy-as-gradient insight from Karpathy, applied to a personal cognitive system. The user doesn't have to choose between "manual control" and "runaway autonomy." They get a dial.

### 4.2 The Self-Improving Meta-Loop

v2 adds a new layer — conceptually L8 — that sits above reflection. The meta-loop:

1. **Observes** every intention, every action, every user response (approval, dismissal, silence).
2. **Evaluates** utility: did this intention lead to something useful? Did this notification add value or noise?
3. **Learns** from outcomes: adjusts prompt strategies, tunes thresholds, suppresses patterns that produce noise, amplifies patterns that produce insight.
4. **Reports** its learning trajectory: transparency is mandatory. The user can always see what changed, why, and revert.

This is the self-improvement loop that v1 lacks. It is not AGI-recursive — the meta-loop is constrained to the system's own parameters and behaviors, not to rewriting its own fundamental architecture. But within those bounds, it makes CoreMind better at being CoreMind.

### 4.3 The JEPA-Inspired Embedding World

v2 replaces the text-dump world snapshot with an **embedding-native world representation**. Instead of sending the LLM "living room temperature: 22.4°C, kitchen humidity: 47%, bedroom lights: off..." as raw text, v2:

1. Maintains learned embeddings for every entity in the world model — rooms, devices, people, recurring events, physiological states.
2. Computes anomaly scores in embedding space — a spike in bedroom temperature at 3 AM is meaningful not because the number changed, but because the *pattern* deviated from learned norms.
3. Projects the current world state into a compact latent representation that captures relational structure, temporal dynamics, and anomaly signals — then feeds this to the LLM as context, not as a raw data dump.

The result: each intention cycle costs 60–80% fewer tokens for the world snapshot. More importantly, the system *understands* the world in a compressed, relational way rather than re-parsing it from text every cycle. This is the JEPA insight at the scale of a personal home.

### 4.4 The Auto-Investigation Loop

v1 generates questions and then... waits. v2 generates questions and then **actively investigates them**.

When CoreMind v2 forms a hypothesis — "the user's sleep quality seems to be degrading on weekdays" or "the living room temperature spikes every afternoon around 3 PM" — it doesn't just drop it into the intention queue. It:

1. Formulates the question as a testable hypothesis.
2. Queries historical data, cross-references with other domains (is sleep degradation correlated with late-night phone use? with late dinners? with temperature?).
3. Generates a finding — confirmed, contradicted, or inconclusive.
4. If confirmed, surfaces it to the user as an actionable insight. If inconclusive, continues monitoring with adjusted parameters.
5. Writes the finding into episodic memory, where it becomes part of the world model for future cycles.

This closes the loop between observation, question, investigation, and knowledge. The system doesn't just ask "why?" — it answers.

### 4.5 The Unified Actuator Surface

v1's effector layer requires each plugin to manually register its available actions. If a user installs a new smart device in Home Assistant, CoreMind doesn't automatically know it can control it.

v2 introduces **auto-discovery**: plugins describe their capabilities through a standardized manifest, and the effector layer builds a unified actuator surface dynamically. New devices, new APIs, new capabilities become available to the intention layer without code changes. The system also learns which actuators have been used successfully, which ones the user tends to approve or reject, and incorporates this into the autonomy slider's per-device calibration.

This is the foundation for a system that grows with its user's environment rather than requiring manual reconfiguration every time something changes.

---

## 5. v1 vs v2 — Capability Comparison

| Capability | v1 (Living Intelligence) | v2 (Cognition) |
|---|---|---|
| **Continuous observation** | ✅ 10 plugins, real-time WorldEvent stream | ✅ Same plugins, plus embedding-native ingestion |
| **World model** | ✅ Relational graph (SurrealDB), entity/event/attribute | ✅ Same graph + learned embeddings, anomaly detection in latent space |
| **Memory** | ✅ Episodic (Qdrant), semantic, procedural | ✅ Same memory + auto-populated from investigations |
| **Reasoning** | ✅ LLM over text snapshots of world state | ✅ LLM over compressed embedding snapshots — 60–80% token reduction |
| **Intention formation** | ✅ Self-prompting loop, salience scoring | ✅ Same + learned salience from meta-loop feedback |
| **Graduated agency** | ✅ Safe / Suggest / Ask per action class | ✅ Per-domain autonomy slider (0–5), earnable trust |
| **Action execution** | ✅ Signed, journaled, reversible | ✅ Same + auto-discovered actuator surface |
| **Reflection** | ✅ Weekly retrospective reports | ✅ Same + forward-looking improvement recommendations from meta-loop |
| **Self-improvement** | ❌ None — static prompts, fixed thresholds | ✅ Meta-loop: observe → evaluate → learn → tune |
| **Investigation** | ❌ Questions generated, stale ones pruned | ✅ Active investigation loop: hypothesis → query → finding → memory |
| **Token efficiency** | ❌ Raw text dumps to LLM every cycle | ✅ Embedding-native world snapshots |
| **Dynamic adaptation** | ❌ Config-file changes require human edits | ✅ System tunes its own parameters, user retains veto |
| **Plugin extensibility** | ✅ gRPC plugin protocol, multi-language | ✅ Same + auto-discovered actuator capabilities |

---

## 6. The Big Question v2 Answers

v1 answered: **"What would an intelligence that lived with me notice about my life?"**

v2 answers a deeper question: **"How does the system get better at understanding me — without me having to teach it?"**

This is the difference between a system that sees and a system that learns. v1 saw that Guillaume's sleep degraded on weekdays. v2 notices the pattern, investigates the correlation with late-night phone use and bedroom temperature, forms a hypothesis, tests it against historical data, and — if confirmed — surfaces it: "Your sleep quality drops 23% on nights when the bedroom is above 22°C. Want me to pre-cool it starting at 9 PM on weekdays?"

And then, when Guillaume says yes, the system notes that this kind of proactive climate suggestion was well-received, and adjusts its salience model to pay more attention to environmental factors that correlate with health outcomes.

This is not a chatbot that gets better at chatting. This is a cognitive system that gets better at cognition. It learns what matters, what doesn't, and how to calibrate itself to the specific human it lives with.

---

## 7. Non-Negotiable Principles

v2 inherits all of v1's ethical commitments and adds one more. These are baked into the core, not bolted on:

### 7.1 Sovereignty

User data never leaves the machine by default. The embedding world model, the meta-loop, the auto-investigation engine — all run locally. Cloud is an explicit, opt-in plugin. There is no training on user data by remote models. The system learns *about* its user, but it never *exports* its user.

This is especially critical for v2 because the meta-loop generates a much richer model of the user's behavior than v1 ever could. That model stays local. Period.

### 7.2 Reversibility

Every autonomous action — including every parameter change made by the meta-loop — is signed, logged, and reversible. The user can always see *what changed* in their system's behavior, *why* it changed (with a link to the specific intention or investigation that prompted it), and *revert* any change with a single command.

The autonomy slider can be dialed down — or to zero — at any time. The system cannot lock the user out of their own controls. The meta-loop's parameter adjustments are suggestions that take effect, not irreversible transformations.

### 7.3 Safety

The forced-approval classes from v1 remain: finance, outbound messaging, credential changes, modifications to CoreMind's own safety mechanisms. These cannot be downgraded by any plugin, any intention, or any meta-loop learning. The autonomy slider cannot reach into these classes — they are permanently locked at "ask."

v2 adds a new safety commitment: the meta-loop's learning trajectory is transparent and auditable. Every parameter change is versioned. The user receives a weekly "learning report" alongside the v1 reflection report: what the system learned about the user this week, what it changed about its own behavior, and why. No silent drift. No opaque tuning.

---

## 8. Timeline and Approach

v2 is not a greenfield project. It builds directly on the working v1 codebase. The 86 Python modules and 46 test files are the foundation, not the obstacle.

| Phase | New Capability | Estimated Duration |
|---|---|---|
| **Phase 1** | Autonomy Slider — per-domain graduated agency | ~1 week |
| **Phase 2** | Self-Improving Meta-Loop — observe, evaluate, learn, tune | ~1–1.5 weeks |
| **Phase 3** | JEPA-Inspired Embedding World — learned latent representations | ~1 week |
| **Phase 4** | Auto-Investigation Loop — hypothesis → test → find → remember | ~1 week |
| **Phase 5** | Unified Actuator Surface — auto-discovery and dynamic effector registry | ~1 week |

Each phase is independently shippable. Phase 1 ships alone, the user has per-domain autonomy control. Phase 2 ships, and the system starts learning. Phase 3 ships, and token costs drop by 60–80%. Each phase delivers value on its own; together, they transform CoreMind from a daemon into a learner.

Total: **4–6 weeks** for a complete v2 MVP, with ongoing iteration after.

---

## 9. Why This Matters

The AI industry in 2026 is dominated by two conversations: **scaling** (bigger models, more data, more GPUs) and **agency** (systems that act autonomously, with all the safety and alignment questions that raises).

CoreMind v2 sits at a different intersection. It is not about scaling — the models it uses are commodity LLMs accessed through LiteLLM, and the JEPA-inspired embeddings are lightweight, local, and personal. It is about **adaptation**: the ability of a system to get better at its job over time, within a single user's context, without compromising sovereignty.

This is the problem that frontier labs aren't solving. GPT-7 will not know your home's temperature patterns better than CoreMind v2, because CoreMind v2 lives there. Claude 5 will not understand your sleep patterns better than CoreMind v2, because CoreMind v2 has watched them for months. The frontier builds general intelligence; CoreMind builds **personal intelligence** — a system that understands one specific world (yours) better than any general system ever could.

v2 is the step from a system that *sees* your world to a system that *learns* your world.

---

**Next step:** [`ARCHITECTURE.md`](ARCHITECTURE.md) — the full technical design for v2.
