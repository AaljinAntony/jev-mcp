# Comprehensive Comparison: Official TypeSafe Agent Skill vs. Jev MCP Server (`jev-engine`)

This report provides an in-depth architectural and functional comparison between the **Official TypeSafe Agent Skill** ([https://docs.typesafe.ai/agent-skill](https://docs.typesafe.ai/agent-skill)) and this workspace's **`jev-engine` MCP Server** (`D:\mcp\jev-typesafe-mcp`).

---

## 1. Executive Summary

The official **TypeSafe Agent Skill** and your **`jev-engine` MCP Server** operate at two fundamentally different layers of the AI engineering stack:

* **Official TypeSafe Agent Skill is a *Prompt & Knowledge Layer* (System Prompt Extension):**
  It is a Markdown instruction set (`SKILL.md`) injected into the coding agent's context window (Claude Code, Cursor, Codex, OpenCode, Antigravity, etc.). Its purpose is to teach the LLM **how to write application code that uses TypeSafe AI**—explaining the `typesafe-sdk`, primitive types (`Choice`, `Score`, `Noul`), architectural patterns, and preventing API hallucinations. It contains **no executable code, no server, and no runtime tools**.
* **Your MCP (`jev-typesafe-mcp`) is an *Operational Runtime Tool & Guardrail Layer* (Executable stdio JSON-RPC):**
  It is an active Python-based Model Context Protocol (MCP) server that puts TypeSafe's Jev model **to work in real time** to supervise, optimize, and guardrail your coding agent through 4 deterministic tools (`guardrail_command`, `search_agent_skills`, `search_target_files`, `select_model_tier`) alongside an OpenCode hook plugin.

---

## 2. Side-by-Side Comparison Matrix

| Dimension | Official TypeSafe Agent Skill (`docs.typesafe.ai/agent-skill`) | Your MCP Server (`jev-typesafe-mcp`) |
| :--- | :--- | :--- |
| **Category** | Agent Skill / Context Prompt (`SKILL.md`) | MCP Tool Server + Runtime Plugin (`MCPServer` stdio) |
| **Primary Goal** | Teach the coding agent how to write TypeSafe API code in user projects | Use Jev in real time to audit commands, prune skills, pick files, and switch models |
| **Role of Jev** | The **subject** of the code being written | The **runtime decision engine** powering agent tools |
| **Runtime Footprint** | Static Markdown text loaded into the LLM context window | Active Python daemon (`mcp==2.2.0`) running over stdio JSON-RPC |
| **API Calls** | **None by default** (only if the LLM writes and executes code) | **Direct live calls** via `TypeSafeClient.system_one(state, questions)` |
| **Tools Provided** | None (it is purely context/documentation) | 4 tools: `guardrail_command`, `search_agent_skills`, `search_target_files`, `select_model_tier` |
| **Safety / Guardrails** | Advisory text (recommends best practices) | **Active, fail-closed enforcement** with probability thresholds |
| **Token Impact** | Consumes LLM context window tokens to explain docs/syntax | **Saves LLM tokens** by pruning files & skills before sending them to the model |
| **Offline / Test Mode** | None (relies on agent training + live docs access) | Built-in offline deterministic judge (`mock.py` via `JEV_MCP_MOCK=1`) + 58 test cases |
| **Supported Clients** | Claude Code, Codex, any skill-aware agent | OpenCode, Claude Code, Cursor, Antigravity, any MCP client |

---

## 3. Deep Dive: Official TypeSafe Agent Skill

### 3.1 What It Is
The official agent skill distributed by TypeSafe AI (via `claude plugin install typesafe@typesafe-ai` or `npx skills add typesafe-ai/skills --skill typesafe-ai`) is a specialized system prompt and reference library.

It consists of:
1. **`SKILL.md`**: Instruction file directing the agent to read the live docs via `https://docs.typesafe.ai/llms.txt`.
2. **API & Primitive Guidelines**: Rules preventing the agent from inventing classes like `JevClient` or using incorrect parameters (e.g., reminding the model that `Choice(criteria={...})` requires a dictionary mapping, not a list).
3. **Architectural Recipes**: Context on patterns like speculative fan-out, confidence-gated routing, composite scoring, and intent routing.

### 3.2 What It Does
* When you ask an agent: *"Refactor our auth service to use TypeSafe for risk scoring,"* the skill ensures the LLM knows the current SDK syntax (`from typesafe_sdk import TypeSafeClient, Score`).
* Prevents API hallucinations and encourages the agent to check TypeSafe cookbooks.
* Injects best practices for question design, threshold setting, and state fitting into the prompt.

### 3.3 What It Does NOT Do
* It cannot intercept or inspect commands that the agent runs in your terminal.
* It cannot automatically filter your repository files or route your agent to different LLM tiers.
* It does not run background processes or expose JSON-RPC tools to the client.

---

## 4. Deep Dive: Your MCP (`jev-typesafe-mcp`)

### 4.1 What It Is
Your project is an end-to-end, production-grade **decision and guardrail infrastructure** that uses TypeSafe's Jev model to govern the coding agent itself.

Key components:
* **The MCP Server (`jev_mcp.py`):** Registers 4 tools with standard MCP schemas and maps exceptions into typed, fail-closed error envelopes.
* **The Core Decision Engine (`jev_engine.py`):** Translates agent tasks and shell commands into Jev `Noul` and `Choice` evaluations.
* **Fail-Closed Validation (`jev_validation.py`):** Validates responses against question schemas *before* any policy number is read—meaning malformed responses never accidentally pass as `safe: true`.
* **Policy Engine (`policy.py`):** Calibrates confidence, applies risk thresholds (`auto`, `review`, `escalate`), and handles context truncation.
* **Context Fitting (`limits.py`):** Bounds token budgets and truncates state gracefully (`MAX_CHOICE_OPTIONS = 64`, `MAX_CONTENT_CHARS = 6000`).
* **Deterministic Offline Judge (`mock.py`):** Runs without API keys for testing and CI/CD via `JEV_MCP_MOCK=1`.
* **OpenCode Hook Plugin (`config/jev-plugin.example.js`):** A `chat.message` hook that dynamically forces model tier switching and injects the winning skill into the context window.

### 4.2 The 4 Tools Explained

#### 1. `guardrail_command(command: str) -> dict`
* **Purpose:** Pre-execution audit for terminal shell commands.
* **Jev Primitives:** Two `Noul` questions (`is_destructive`, `modifies_git`).
* **Thresholds:** A command is judged `safe: true` only if `action == "auto"` **and** `destructive_prob < 0.20` **and** `git_modify_prob < 0.20`. A confident destructive judgment (`>= 0.50`) escalates; `>= 0.20` reviews.
* **Protection:** Catches `rm -rf`, `DROP TABLE`, `git push --force`, or accidental disk wipes before execution.

#### 2. `search_agent_skills(task: str, root_dir: str = ".") -> dict`
* **Purpose:** Dynamic context pruning and skill routing.
* **Scan Paths:** Workspace directories (`.agents/skills`, `.agents/workflows`, `.opencode/skills`, `skills`, etc.).
* **Jev Primitives:** Multi-stage `Choice` evaluations (`primary`, `secondary`, `tertiary`) with probability clustering (secondary picks `>= 0.12`).
* **Benefit:** Inlines only the relevant Markdown skill (up to 6,000 characters), preventing token bloat in long conversations.

#### 3. `search_target_files(task: str, root_dir: str = ".") -> dict`
* **Purpose:** Fast workspace file selector.
* **Filtering:** Recursively scans repo files while automatically ignoring binaries, media, `.git`, `node_modules`, `.venv`, and build artifacts.
* **Jev Primitives:** `Choice` evaluation over candidate files with a `"none"` escape hatch.
* **Benefit:** Returns an `exists` verdict (`answered`, `absent`, `partial`), telling the agent exactly which source files to inspect or modify without guessing.

#### 4. `select_model_tier(task: str) -> dict`
* **Purpose:** Dynamic LLM model tier selection based on task complexity.
* **Tiers:** `fast` (typos, lookups, docs), `balanced` (standard bugs, test cases), `frontier` (architecture, multi-file refactoring).
* **Forced Switching:** Coupled with the OpenCode `chat.message` plugin hook (`jev-plugin.js`), it mutates `output.message.model` to force the IDE to switch models per task, optimizing cost and performance.

---

## 5. Architectural & Technical Strengths of Your MCP

1. **Strict Fail-Closed Design:**
   In `jev_validation.py` and `policy.py`, malformed responses, truncated states, or low-confidence evaluations immediately downgrade actions to `review` or `escalate`. A command is never judged safe by accident or timeout.
2. **Context Budgeting:**
   `limits.py` enforces character and token limits (`MAX_CHOICE_OPTIONS = 64`, `MAX_CONTENT_CHARS = 6000`), preventing prompt overflow and API crashes.
3. **Zero API Key Dependency for Testing:**
   `mock.py` allows the entire MCP server and test suite (58 pytest tests) to run completely offline without spending API credits or needing network access.
4. **Real Agent Integration (Forced Switching):**
   Unlike basic MCP servers that merely return recommendations, your implementation couples with an OpenCode hook (`jev-plugin.js`) that mutates `output.message.model` to enforce runtime model changes.

---

## 6. Which Is Better?

Because they solve different problems, **neither replaces the other**. The question of "which is better" depends on your immediate goal:

### Scenario A: You want to safeguard and optimize your agent's workflow
👉 **Your MCP (`jev-typesafe-mcp`) is far superior.**
* The official TypeSafe skill cannot protect your terminal from destructive commands.
* The official skill cannot automatically inspect your project tree, select target files, or prune skills.
* The official skill cannot dynamically force-switch your agent's LLM model based on task complexity.
* Your MCP provides concrete, measurable runtime utility, strict fail-closed safety, and offline testability.

### Scenario B: You want your agent to write application code using TypeSafe AI
👉 **The Official TypeSafe Agent Skill is better.**
* If you tell an agent *"write a Python pipeline that uses TypeSafe to moderate user reviews"*, your MCP server tools won't teach the agent how to write that code.
* The official skill gives the LLM the required syntax, classes, and cookbooks to generate bug-free TypeSafe client integrations.

---

## 7. How They Work Together (The Ideal Synergy)

The two tools are complementary and form a complete developer workflow:

```
┌─────────────────────────────────────────────────────────────┐
│                       Your Coding Agent                     │
│  (Claude Code, OpenCode, Cursor, Codex, or Antigravity)     │
└──────────────┬──────────────────────────────┬───────────────┘
               │                              │
     Prompt Instructions                MCP JSON-RPC Tools
               │                              │
               ▼                              ▼
┌──────────────────────────────┐ ┌─────────────────────────────┐
│ Official TypeSafe Skill      │ │ Your MCP (jev-typesafe-mcp) │
│ (https://docs.typesafe.ai)   │ │ (D:\mcp\jev-typesafe-mcp)   │
├──────────────────────────────┤ ├─────────────────────────────┤
│ • Injects SDK docs into LLM  │ │ • guardrail_command         │
│ • Guides prompt & primitive  │ │ • search_agent_skills       │
│   selection                  │ │ • search_target_files       │
│ • Prevents API hallucination │ │ • select_model_tier         │
│ • Explains cookbooks/recipes │ │ • Fail-closed safety engine │
└──────────────────────────────┘ └─────────────────────────────┘
               │                              │
               ▼                              ▼
  Agent writes correct TypeSafe    Agent operates safely, efficiently,
      code in your codebase           and with minimal token waste
```

### Recommendation
1. **Install the official skill** in your agent environment so that whenever you ask your agent to build applications using TypeSafe, it writes syntactically and architecturally sound code.
2. **Keep your `jev-engine` MCP server active** so that whenever your agent executes terminal commands, searches workspace files, retrieves local skills, or routes models, your Jev engine acts as its real-time runtime governor.
