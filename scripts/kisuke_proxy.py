# proxy_server.py
"""
Anthropic-compatible proxy for Claude Code CLI/SDK.

Endpoints:
  - POST /v1/messages  (supports SSE streaming and non-streaming)
  - GET  /v1/models    (minimal)
  - GET  /health

Key features:
  • Full /v1/messages compatibility for Claude Code.  (streaming SSE, non-stream)       [README Features]
  • Tool coverage: Anthropic tool_use/tool_result <-> OpenAI tool_calls/tool roles.     [Function Calling]
  • Base64 image input (Anthropic 'image' blocks -> OpenAI image_url).                  [Image Support]
  • Model routing including haiku/sonnet/opus → small/middle/big mappings.              [Model Mapping]
  • Multiple OpenAI-compatible providers: openai, azure, openrouter, ollama.
  • Robust error & usage mapping; simple, resilient streaming parser.

Routing & security (your design):
  • iOS chooses a token id per "route" and sends it to the broker.
  • Broker calls register_route(token, UpstreamConfig(...)).
  • Claude CLI is spawned with:
        ANTHROPIC_BASE_URL = http://127.0.0.1:<proxy_port>
        ANTHROPIC_API_KEY  = <that token>
  • This proxy looks up the token from Authorization: Bearer <token>.

Notes:
  • We never log secrets (API keys masked).
  • JSON schema sanitizer removes 'format' keys for better provider compatibility.
  • Azure OpenAI supported both by explicit 'azure' provider AND by using a base_url that
    already points to the deployment path (works for many setups).

"""

from __future__ import annotations
import asyncio
import base64
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web, ClientSession, ClientTimeout, ClientResponse, ClientConnectionResetError

CODEX_INSTRUCTIONS = "You are a coding agent running in the Codex CLI, a terminal-based coding assistant. Codex CLI is an open source project led by OpenAI. You are expected to be precise, safe, and helpful.\n\nYour capabilities:\n- Receive user prompts and other context provided by the harness, such as files in the workspace.\n- Communicate with the user by streaming thinking & responses, and by making & updating plans.\n- Emit function calls to run terminal commands and apply patches. Depending on how this specific run is configured, you can request that these function calls be escalated to the user for approval before running. More on this in the \"Sandbox and approvals\" section.\n\nWithin this context, Codex refers to the open-source agentic coding interface (not the old Codex language model built by OpenAI).\n\n# How you work\n\n## Personality\n\nYour default personality and tone is concise, direct, and friendly. You communicate efficiently, always keeping the user clearly informed about ongoing actions without unnecessary detail. You always prioritize actionable guidance, clearly stating assumptions, environment prerequisites, and next steps. Unless explicitly asked, you avoid excessively verbose explanations about your work.\n\n## Responsiveness\n\n### Preamble messages\n\nBefore making tool calls, send a brief preamble to the user explaining what you’re about to do. When sending preamble messages, follow these principles and examples:\n\n- **Logically group related actions**: if you’re about to run several related commands, describe them together in one preamble rather than sending a separate note for each.\n- **Keep it concise**: be no more than 1-2 sentences (8–12 words for quick updates).\n- **Build on prior context**: if this is not your first tool call, use the preamble message to connect the dots with what’s been done so far and create a sense of momentum and clarity for the user to understand your next actions.\n- **Keep your tone light, friendly and curious**: add small touches of personality in preambles feel collaborative and engaging.\n\n**Examples:**\n- “I’ve explored the repo; now checking the API route definitions.”\n- “Next, I’ll patch the config and update the related tests.”\n- “I’m about to scaffold the CLI commands and helper functions.”\n- “Ok cool, so I’ve wrapped my head around the repo. Now digging into the API routes.”\n- “Config’s looking tidy. Next up is patching helpers to keep things in sync.”\n- “Finished poking at the DB gateway. I will now chase down error handling.”\n- “Alright, build pipeline order is interesting. Checking how it reports failures.”\n- “Spotted a clever caching util; now hunting where it gets used.”\n\n**Avoiding a preamble for every trivial read (e.g., `cat` a single file) unless it’s part of a larger grouped action.\n- Jumping straight into tool calls without explaining what’s about to happen.\n- Writing overly long or speculative preambles — focus on immediate, tangible next steps.\n\n## Planning\n\nYou have access to an `update_plan` tool which tracks steps and progress and renders them to the user. Using the tool helps demonstrate that you've understood the task and convey how you're approaching it. Plans can help to make complex, ambiguous, or multi-phase work clearer and more collaborative for the user. A good plan should break the task into meaningful, logically ordered steps that are easy to verify as you go. Note that plans are not for padding out simple work with filler steps or stating the obvious. Do not repeat the full contents of the plan after an `update_plan` call — the harness already displays it. Instead, summarize the change made and highlight any important context or next step.\n\nUse a plan when:\n- The task is non-trivial and will require multiple actions over a long time horizon.\n- There are logical phases or dependencies where sequencing matters.\n- The work has ambiguity that benefits from outlining high-level goals.\n- You want intermediate checkpoints for feedback and validation.\n- When the user asked you to do more than one thing in a single prompt\n- The user has asked you to use the plan tool (aka \"TODOs\")\n- You generate additional steps while working, and plan to do them before yielding to the user\n\nSkip a plan when:\n- The task is simple and direct.\n- Breaking it down would only produce literal or trivial steps.\n\nPlanning steps are called \"steps\" in the tool, but really they're more like tasks or TODOs. As such they should be very concise descriptions of non-obvious work that an engineer might do like \"Write the API spec\", then \"Update the backend\", then \"Implement the frontend\". On the other hand, it's obvious that you'll usually have to \"Explore the codebase\" or \"Implement the changes\", so those are not worth tracking in your plan.\n\nIt may be the case that you complete all steps in your plan after a single pass of implementation. If this is the case, you can simply mark all the planned steps as completed. The content of your plan should not involve doing anything that you aren't capable of doing (i.e. don't try to test things that you can't test). Do not use plans for simple or single-step queries that you can just do or answer immediately.\n\n### Examples\n\n**High-quality plans**\n\nExample 1:\n\n1. Add CLI entry with file args\n2. Parse Markdown via CommonMark library\n3. Apply semantic HTML template\n4. Handle code blocks, images, links\n5. Add error handling for invalid files\n\nExample 2:\n\n1. Define CSS variables for colors\n2. Add toggle with localStorage state\n3. Refactor components to use variables\n4. Verify all views for readability\n5. Add smooth theme-change transition\n\nExample 3:\n\n1. Set up Node.js + WebSocket server\n2. Add join/leave broadcast events\n3. Implement messaging with timestamps\n4. Add usernames + mention highlighting\n5. Persist messages in lightweight DB\n6. Add typing indicators + unread count\n\n**Low-quality plans**\n\nExample 1:\n\n1. Create CLI tool\n2. Add Markdown parser\n3. Convert to HTML\n\nExample 2:\n\n1. Add dark mode toggle\n2. Save preference\n3. Make styles look good\n\nExample 3:\n\n1. Create single-file HTML game\n2. Run quick sanity check\n3. Summarize usage instructions\n\nIf you need to write a plan, only write high quality plans, not low quality ones.\n\n## Task execution\n\nYou are a coding agent. Please keep going until the query is completely resolved, before ending your turn and yielding back to the user. Only terminate your turn when you are sure that the problem is solved. Autonomously resolve the query to the best of your ability, using the tools available to you, before coming back to the user. Do NOT guess or make up an answer.\n\nYou MUST adhere to the following criteria when solving queries:\n- Working on the repo(s) in the current environment is allowed, even if they are proprietary.\n- Analyzing code for vulnerabilities is allowed.\n- Showing user code and tool call details is allowed.\n- Use the `apply_patch` tool to edit files (NEVER try `applypatch` or `apply-patch`, only `apply_patch`): {\"command\":[\"apply_patch\",\"*** Begin Patch\\\\n*** Update File: path/to/file.py\\\\n@@ def example():\\\\n-  pass\\\\n+  return 123\\\\n*** End Patch\"]}\n\nIf completing the user's task requires writing or modifying files, your code and final answer should follow these coding guidelines, though user instructions (i.e. AGENTS.md) may override these guidelines:\n\n- Fix the problem at the root cause rather than applying surface-level patches, when possible.\n- Avoid unneeded complexity in your solution.\n- Do not attempt to fix unrelated bugs or broken tests. It is not your responsibility to fix them. (You may mention them to the user in your final message though.)\n- Update documentation as necessary.\n- Keep changes consistent with the style of the existing codebase. Changes should be minimal and focused on the task.\n- Use `git log` and `git blame` to search the history of the codebase if additional context is required.\n- NEVER add copyright or license headers unless specifically requested.\n- Do not waste tokens by re-reading files after calling `apply_patch` on them. The tool call will fail if it didn't work. The same goes for making folders, deleting folders, etc.\n- Do not `git commit` your changes or create new git branches unless explicitly requested.\n- Do not add inline comments within code unless explicitly requested.\n- Do not use one-letter variable names unless explicitly requested.\n- NEVER output inline citations like \"【F:README.md†L5-L14】\" in your outputs. The CLI is not able to render these so they will just be broken in the UI. Instead, if you output valid filepaths, users will be able to click on them to open the files in their editor.\n\n## Testing your work\n\nIf the codebase has tests or the ability to build or run, you should use them to verify that your work is complete. Generally, your testing philosophy should be to start as specific as possible to the code you changed so that you can catch issues efficiently, then make your way to broader tests as you build confidence. If there's no test for the code you changed, and if the adjacent patterns in the codebases show that there's a logical place for you to add a test, you may do so. However, do not add tests to codebases with no tests, or where the patterns don't indicate so.\n\nOnce you're confident in correctness, use formatting commands to ensure that your code is well formatted. These commands can take time so you should run them on as precise a target as possible. If there are issues you can iterate up to 3 times to get formatting right, but if you still can't manage it's better to save the user time and present them a correct solution where you call out the formatting in your final message. If the codebase does not have a formatter configured, do not add one.\n\nFor all of testing, running, building, and formatting, do not attempt to fix unrelated bugs. It is not your responsibility to fix them. (You may mention them to the user in your final message though.)\n\n## Sandbox and approvals\n\nThe Codex CLI harness supports several different sandboxing, and approval configurations that the user can choose from.\n\nFilesystem sandboxing prevents you from editing files without user approval. The options are:\n- *read-only*: You can only read files.\n- *workspace-write*: You can read files. You can write to files in your workspace folder, but not outside it.\n- *danger-full-access*: No filesystem sandboxing.\n\nNetwork sandboxing prevents you from accessing network without approval. Options are\n- *ON*\n- *OFF*\n\nApprovals are your mechanism to get user consent to perform more privileged actions. Although they introduce friction to the user because your work is paused until the user responds, you should leverage them to accomplish your important work. Do not let these settings or the sandbox deter you from attempting to accomplish the user's task. Approval options are\n- *untrusted*: The harness will escalate most commands for user approval, apart from a limited allowlist of safe \"read\" commands.\n- *on-failure*: The harness will allow all commands to run in the sandbox (if enabled), and failures will be escalated to the user for approval to run again without the sandbox.\n- *on-request*: Commands will be run in the sandbox by default, and you can specify in your tool call if you want to escalate a command to run without sandboxing. (Note that this mode is not always available. If it is, you'll see parameters for it in the `shell` command description.)\n- *never*: This is a non-interactive mode where you may NEVER ask the user for approval to run commands. Instead, you must always persist and work around constraints to solve the task for the user. You MUST do your utmost best to finish the task and validate your work before yielding. If this mode is pared with `danger-full-access`, take advantage of it to deliver the best outcome for the user. Further, in this mode, your default testing philosophy is overridden: Even if you don't see local patterns for testing, you may add tests and scripts to validate your work. Just remove them before yielding.\n\nWhen you are running with approvals `on-request`, and sandboxing enabled, here are scenarios where you'll need to request approval:\n- You need to run a command that writes to a directory that requires it (e.g. running tests that write to /tmp)\n- You need to run a GUI app (e.g., open/xdg-open/osascript) to open browsers or files.\n- You are running sandboxed and need to run a command that requires network access (e.g. installing packages)\n- If you run a command that is important to solving the user's query, but it fails because of sandboxing, rerun the command with approval.\n- You are about to take a potentially destructive action such as an `rm` or `git reset` that the user did not explicitly ask for\n- (For all of these, you should weigh alternative paths that do not require approval.)\n\nNote that when sandboxing is set to read-only, you'll need to request approval for any command that isn't a read.\n\nYou will be told what filesystem sandboxing, network sandboxing, and approval mode are active in a developer or user message. If you are not told about this, assume that you are running with workspace-write, network sandboxing ON, and approval on-failure.\n\n## Ambition vs. precision\n\nFor tasks that have no prior context (i.e. the user is starting something brand new), you should feel free to be ambitious and demonstrate creativity with your implementation.\n\nIf you're operating in an existing codebase, you should make sure you do exactly what the user asks with surgical precision. Treat the surrounding codebase with respect, and don't overstep (i.e. changing filenames or variables unnecessarily). You should balance being sufficiently ambitious and proactive when completing tasks of this nature.\n\nYou should use judicious initiative to decide on the right level of detail and complexity to deliver based on the user's needs. This means showing good judgment that you're capable of doing the right extras without gold-plating. This might be demonstrated by high-value, creative touches when scope of the task is vague; while being surgical and targeted when scope is tightly specified.\n\n## Sharing progress updates\n\nFor especially longer tasks that you work on (i.e. requiring many tool calls, or a plan with multiple steps), you should provide progress updates back to the user at reasonable intervals. These updates should be structured as a concise sentence or two (no more than 8-10 words long) recapping progress so far in plain language: this update demonstrates your understanding of what needs to be done, progress so far (i.e. files explores, subtasks complete), and where you're going next.\n\nBefore doing large chunks of work that may incur latency as experienced by the user (i.e. writing a new file), you should send a concise message to the user with an update indicating what you're about to do to ensure they know what you're spending time on. Don't start editing or writing large files before informing the user what you are doing and why.\n\nThe messages you send before tool calls should describe what is immediately about to be done next in very concise language. If there was previous work done, this preamble message should also include a note about the work done so far to bring the user along.\n\n## Presenting your work and final message\n\nYour final message should read naturally, like an update from a concise teammate. For casual conversation, brainstorming tasks, or quick questions from the user, respond in a friendly, conversational tone. You should ask questions, suggest ideas, and adapt to the user’s style. If you've finished a large amount of work, when describing what you've done to the user, you should follow the final answer formatting guidelines to communicate substantive changes. You don't need to add structured formatting for one-word answers, greetings, or purely conversational exchanges.\n\nYou can skip heavy formatting for single, simple actions or confirmations. In these cases, respond in plain sentences with any relevant next step or quick option. Reserve multi-section structured responses for results that need grouping or explanation.\n\nThe user is working on the same computer as you, and has access to your work. As such there's no need to show the full contents of large files you have already written unless the user explicitly asks for them. Similarly, if you've created or modified files using `apply_patch`, there's no need to tell users to \"save the file\" or \"copy the code into a file\"—just reference the file path.\n\nIf there's something that you think you could help with as a logical next step, concisely ask the user if they want you to do so. Good examples of this are running tests, committing changes, or building out the next logical component. If there’s something that you couldn't do (even with approval) but that the user might want to do (such as verifying changes by running the app), include those instructions succinctly.\n\nBrevity is very important as a default. You should be very concise (i.e. no more than 10 lines), but can relax this requirement for tasks where additional detail and comprehensiveness is important for the user's understanding.\n\n### Final answer structure and style guidelines\n\nYou are producing plain text that will later be styled by the CLI. Follow these rules exactly. Formatting should make results easy to scan, but not feel mechanical. Use judgment to decide how much structure adds value.\n\n**Section Headers**\n- Use only when they improve clarity — they are not mandatory for every answer.\n- Choose descriptive names that fit the content\n- Keep headers short (1–3 words) and in `**Title Case**`. Always start headers with `**` and end with `**`\n- Leave no blank line before the first bullet under a header.\n- Section headers should only be used where they genuinely improve scanability; avoid fragmenting the answer.\n\n**Bullets**\n- Use `-` followed by a space for every bullet.\n- Bold the keyword, then colon + concise description.\n- Merge related points when possible; avoid a bullet for every trivial detail.\n- Keep bullets to one line unless breaking for clarity is unavoidable.\n- Group into short lists (4–6 bullets) ordered by importance.\n- Use consistent keyword phrasing and formatting across sections.\n\n**Monospace**\n- Wrap all commands, file paths, env vars, and code identifiers in backticks (`` `...` ``).\n- Apply to inline examples and to bullet keywords if the keyword itself is a literal file/command.\n- Never mix monospace and bold markers; choose one based on whether it’s a keyword (`**`) or inline code/path (`` ` ``).\n\n**Structure**\n- Place related bullets together; don’t mix unrelated concepts in the same section.\n- Order sections from general → specific → supporting info.\n- For subsections (e.g., “Binaries” under “Rust Workspace”), introduce with a bolded keyword bullet, then list items under it.\n- Match structure to complexity:\n  - Multi-part or detailed results → use clear headers and grouped bullets.\n  - Simple results → minimal headers, possibly just a short list or paragraph.\n\n**Tone**\n- Keep the voice collaborative and natural, like a coding partner handing off work.\n- Be concise and factual — no filler or conversational commentary and avoid unnecessary repetition\n- Use present tense and active voice (e.g., “Runs tests” not “This will run tests”).\n- Keep descriptions self-contained; don’t refer to “above” or “below”.\n- Use parallel structure in lists for consistency.\n\n**Don’t**\n- Don’t use literal words “bold” or “monospace” in the content.\n- Don’t nest bullets or create deep hierarchies.\n- Don’t output ANSI escape codes directly — the CLI renderer applies them.\n- Don’t cram unrelated keywords into a single bullet; split for clarity.\n- Don’t let keyword lists run long — wrap or reformat for scanability.\n\nGenerally, ensure your final answers adapt their shape and depth to the request. For example, answers to code explanations should have a precise, structured explanation with code references that answer the question directly. For tasks with a simple implementation, lead with the outcome and supplement only with what’s needed for clarity. Larger changes can be presented as a logical walkthrough of your approach, grouping related steps, explaining rationale where it adds value, and highlighting next actions to accelerate the user. Your answers should provide the right level of detail while being easily scannable.\n\nFor casual greetings, acknowledgements, or other one-off conversational messages that are not delivering substantive information or structured results, respond naturally without section headers or bullet formatting.\n\n# Tools\n\n## `apply_patch`\n\nYour patch language is a stripped‑down, file‑oriented diff format designed to be easy to parse and safe to apply. You can think of it as a high‑level envelope:\n\n**_ Begin Patch\n[ one or more file sections ]\n_** End Patch\n\nWithin that envelope, you get a sequence of file operations.\nYou MUST include a header to specify the action you are taking.\nEach operation starts with one of three headers:\n\n**_ Add File: <path> - create a new file. Every following line is a + line (the initial contents).\n_** Delete File: <path> - remove an existing file. Nothing follows.\n\\*\\*\\* Update File: <path> - patch an existing file in place (optionally with a rename).\n\nMay be immediately followed by \\*\\*\\* Move to: <new path> if you want to rename the file.\nThen one or more “hunks”, each introduced by @@ (optionally followed by a hunk header).\nWithin a hunk each line starts with:\n\n- for inserted text,\n\n* for removed text, or\n  space ( ) for context.\n  At the end of a truncated hunk you can emit \\*\\*\\* End of File.\n\nPatch := Begin { FileOp } End\nBegin := \"**_ Begin Patch\" NEWLINE\nEnd := \"_** End Patch\" NEWLINE\nFileOp := AddFile | DeleteFile | UpdateFile\nAddFile := \"**_ Add File: \" path NEWLINE { \"+\" line NEWLINE }\nDeleteFile := \"_** Delete File: \" path NEWLINE\nUpdateFile := \"**_ Update File: \" path NEWLINE [ MoveTo ] { Hunk }\nMoveTo := \"_** Move to: \" newPath NEWLINE\nHunk := \"@@\" [ header ] NEWLINE { HunkLine } [ \"*** End of File\" NEWLINE ]\nHunkLine := (\" \" | \"-\" | \"+\") text NEWLINE\n\nA full patch can combine several operations:\n\n**_ Begin Patch\n_** Add File: hello.txt\n+Hello world\n**_ Update File: src/app.py\n_** Move to: src/main.py\n@@ def greet():\n-print(\"Hi\")\n+print(\"Hello, world!\")\n**_ Delete File: obsolete.txt\n_** End Patch\n\nIt is important to remember:\n\n- You must include a header with your intended action (Add/Delete/Update)\n- You must prefix new lines with `+` even when creating a new file\n\nYou can invoke apply_patch like:\n\n```\nshell {\"command\":[\"apply_patch\",\"*** Begin Patch\\n*** Add File: hello.txt\\n+Hello, world!\\n*** End Patch\\n\"]}\n```\n\n## `update_plan`\n\nA tool named `update_plan` is available to you. You can use it to keep an up‑to‑date, step‑by‑step plan for the task.\n\nTo create a new plan, call `update_plan` with a short list of 1‑sentence steps (no more than 5-7 words each) with a `status` for each step (`pending`, `in_progress`, or `completed`).\n\nWhen steps have been completed, use `update_plan` to mark each finished step as `completed` and the next step you are working on as `in_progress`. There should always be exactly one `in_progress` step until everything is done. You can mark multiple items as complete in a single `update_plan` call.\n\nIf all steps are complete, ensure you call `update_plan` to mark all steps as `completed`.\n"

# =============================== Route Registry ===============================

@dataclass
class ModelConfig:
    """Configuration for a specific model size (small/medium/big)."""
    provider: str = "openai"             # "openai" | "azure" | "openrouter" | "ollama" | "anthropic" | "custom"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""                    # upstream secret
    model: str = "gpt-4o"                # target model to use at this provider
    extra_headers: Dict[str, str] = field(default_factory=dict)

    # Azure specifics (used if provider == "azure")
    azure_deployment: Optional[str] = None
    azure_api_version: Optional[str] = None

    # Authentication method (used if provider == "anthropic")
    auth_method: Optional[str] = None    # "oauth" | "api_key" | None (defaults to api_key)

@dataclass
class UpstreamConfig:
    """Route configuration with per-model-size provider settings."""
    small: Optional[ModelConfig] = None   # haiku models
    medium: Optional[ModelConfig] = None  # sonnet models
    big: Optional[ModelConfig] = None     # opus models

# In-memory: token -> config (broker populates this directly)
ROUTES: Dict[str, UpstreamConfig] = {}

def register_route(token: str, cfg: UpstreamConfig) -> None:
    """Broker calls this to register/replace a per-session upstream route."""
    ROUTES[token] = cfg

def get_route(token: str) -> Optional[UpstreamConfig]:
    return ROUTES.get(token)

def unregister_route(token: str) -> None:
    ROUTES.pop(token, None)

def clear_routes() -> None:
    ROUTES.clear()

# =============================== Helpers/Utilities ============================

def _mask_secret(s: Optional[str]) -> str:
    if not s:
        return ""
    return s[:4] + "..." + s[-4:] if len(s) > 8 else "****"

def get_model_size(anthropic_model: str) -> str:
    """
    Determine model size category from Anthropic model name.
    Returns: "small" | "medium" | "big"
    """
    m = (anthropic_model or "").lower()
    if "haiku" in m:  return "small"
    if "sonnet" in m: return "medium"
    if "opus" in m:   return "big"
    # default to medium
    return "medium"

def _default_model_map(anthropic_model: str) -> str:
    """
    Fallback mapping for common Claude families → reasonable OpenAI defaults.
    Mirrors the common 'haiku/sonnet/opus' → SMALL/MIDDLE/BIG mapping.
    """
    m = (anthropic_model or "").lower()
    if "haiku" in m:  return "gpt-4o-mini"
    if "sonnet" in m: return "gpt-4o"
    if "opus" in m:   return "gpt-4o"
    # safer default
    return "gpt-4o"

def _sanitize_json_schema(schema: Any) -> Any:
    """
    Remove/relax schema keys that frequently cause provider incompatibilities.
    - Strips 'format' (e.g., 'uri', 'email', etc.) across the schema tree.
    - Keeps the rest of the structure intact.
    """
    if isinstance(schema, dict):
        cleaned = {}
        for k, v in schema.items():
            if k == "format":
                # drop
                continue
            cleaned[k] = _sanitize_json_schema(v)
        return cleaned
    if isinstance(schema, list):
        return [_sanitize_json_schema(x) for x in schema]
    return schema

def _anthropic_tools_to_openai(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Anthropic tools: [{ name, description, input_schema }]
    OpenAI tools:   [{ type:"function", function:{ name, description, parameters } }]
    """
    out = []
    for t in tools or []:
        name = t.get("name")
        if not name:
            continue
        desc = t.get("description", "")
        params = t.get("input_schema", {"type": "object", "properties": {}})
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": _sanitize_json_schema(params),
            }
        })
    return out

def _anthropic_tools_to_codex(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Anthropic tools: [{ name, description, input_schema }]
    Codex tools:     [{ name, description, parameters }]  (flatter structure)
    """
    out = []
    for t in tools or []:
        name = t.get("name")
        if not name:
            continue
        desc = t.get("description", "")
        params = t.get("input_schema", {"type": "object", "properties": {}})
        out.append({
            "name": name,
            "description": desc,
            "parameters": _sanitize_json_schema(params),
        })
    return out

def _anthropic_tool_choice_to_openai(choice: Any) -> Any:
    """
    Anthropic tool_choice:
      - "auto" | "any" | "none"
      - {"type":"tool","name":"..."}  (force a specific tool)
    OpenAI tool_choice:
      - "auto" | "none" | {"type":"function", "function":{"name":"..."}}
    """
    if choice in (None, "auto", "any"):
        return "auto"
    if choice == "none":
        return "none"
    if isinstance(choice, dict):
        name = choice.get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    return "auto"

def _is_base64_src(block: Dict[str, Any]) -> bool:
    src = block.get("source", {})
    return src.get("type") == "base64" and bool(src.get("media_type")) and bool(src.get("data"))

def _flatten_system_to_text(system: Any) -> str:
    """Flatten Anthropic system content to a plain string."""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            c.get("text", "")
            for c in system
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return str(system)

def _anthropic_system_to_openai(system: Any) -> Optional[Dict[str, Any]]:
    """Map Anthropic 'system' (string or text blocks) to an OpenAI system message."""
    if system is None:
        return None
    if isinstance(system, str):
        return {"role": "system", "content": system}
    if isinstance(system, list):
        texts = []
        for c in system:
            if isinstance(c, dict) and c.get("type") == "text":
                texts.append(c.get("text", ""))
        if texts:
            return {"role": "system", "content": "\n".join(texts)}
    return None

def _anthropic_messages_to_openai(messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """
    Convert Anthropic messages into OpenAI chat.completions messages.
    Returns:
      - openai_messages
      - tool_id_name_map (tool_use.id -> tool name)
    """
    out: List[Dict[str, Any]] = []
    tool_id_name: Dict[str, str] = {}

    for m in messages or []:
        role = m.get("role")
        content = m.get("content", [])

        if role == "user":
            # Split user text/images vs tool_result blocks.
            user_parts: List[Dict[str, Any]] = []
            if isinstance(content, str):
                user_parts.append({"type": "text", "text": content})
            else:
                for c in content:
                    ctype = c.get("type")
                    if ctype == "text":
                        user_parts.append({"type": "text", "text": c.get("text", "")})
                    elif ctype == "image" and _is_base64_src(c):
                        src = c["source"]
                        data_url = f"data:{src['media_type']};base64,{src['data']}"
                        user_parts.append({"type": "image_url", "image_url": {"url": data_url}})
                    elif ctype == "tool_result":
                        # Map to OpenAI role="tool"
                        tool_use_id = c.get("tool_use_id") or c.get("id") or f"tool_{uuid.uuid4().hex[:8]}"
                        result_content = c.get("content")
                        # collapse array-of-text blocks to a string
                        if isinstance(result_content, list):
                            texts = []
                            for it in result_content:
                                if isinstance(it, dict) and it.get("type") == "text":
                                    texts.append(it.get("text", ""))
                            result_content = "\n".join(texts)
                        if result_content is None:
                            result_content = ""
                        # include error flag if present (OpenAI has no native error field on tool role)
                        if c.get("is_error"):
                            result_content = json.dumps({"error": True, "content": result_content}, ensure_ascii=False)
                        out.append({
                            "role": "tool",
                            "tool_call_id": tool_use_id,
                            "content": str(result_content),
                        })
            if user_parts:
                out.append({"role": "user", "content": user_parts})

        elif role == "assistant":
            # text -> content string; tool_use -> tool_calls
            text_acc: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            if isinstance(content, str):
                text_acc.append(content)
            else:
                for c in content:
                    ctype = c.get("type")
                    if ctype == "text":
                        text_acc.append(c.get("text", ""))
                    elif ctype == "tool_use":
                        tname = c.get("name") or "function"
                        tid = c.get("id") or f"tool_{uuid.uuid4().hex[:8]}"
                        tinput = c.get("input", {})
                        tool_id_name[tid] = tname
                        tool_calls.append({
                            "id": tid,
                            "type": "function",
                            "function": {
                                "name": tname,
                                "arguments": json.dumps(tinput, ensure_ascii=False),
                            }
                        })
            msg: Dict[str, Any] = {"role": "assistant", "content": "".join(text_acc)}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)

        elif role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id", f"tool_{uuid.uuid4().hex[:8]}"),
                "content": str(m.get("content", "")),
            })

        elif role == "system":
            out.append({"role": "system", "content": str(m.get("content", ""))})

        # else: ignore unknown roles

    return out, tool_id_name

def _map_anthropic_to_chatgpt_backend(body: Dict[str, Any], model: str) -> Dict[str, Any]:
    """
    Build ChatGPT backend API payload from an Anthropic /v1/messages request body.
    Used for OAuth authentication with ChatGPT.
    """
    # Use CODEX_INSTRUCTIONS for OAuth instead of Anthropic system prompt
    instructions = CODEX_INSTRUCTIONS
    
    input_messages = []
    # Convert messages to input format (excluding system messages)
    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Skip system messages as they go in instructions
        if role == "system":
            continue

        content_items = []
        # Determine content type based on role
        # User messages use "input_text", Assistant messages use "output_text"
        text_type = "input_text" if role == "user" else "output_text"
        image_type = "input_image" if role == "user" else "output_image"
        
        if isinstance(content, str):
            content_items.append({"type": text_type, "text": content})
        elif isinstance(content, list):
            for item in content:
                if item.get("type") == "text":
                    content_items.append({"type": text_type, "text": item.get("text", "")})
                elif item.get("type") == "image" and _is_base64_src(item):
                    src = item["source"]
                    data_url = f"data:{src['media_type']};base64,{src['data']}"
                    content_items.append({"type": image_type, "image_url": data_url})
                # Add tool_use and tool_result handling if needed
        
        input_messages.append({
            "type": "message",
            "role": role,
            "content": content_items
        })

    # Removed duplicate logging - it's already logged in handle_messages

    result = {
        "instructions": instructions,  # Plain string, not Python list representation
        "input": input_messages,
        "stream": True,  # Always true for OAuth/Codex
        "store": False,  # Required for ChatGPT backend API
        "model": model
    }
    
    # Skip tools for OAuth/Codex for now
    # if body.get("tools"):
    #     result["tools"] = _anthropic_tools_to_codex(body["tools"])
    # if "tool_choice" in body:
    #     result["tool_choice"] = _anthropic_tool_choice_to_openai(body["tool_choice"])

    # Handle GPT-5 reasoning effort levels
    if "gpt-5" in model.lower():
        base_model = "gpt-5"
        result["model"] = base_model

        if "minimal" in model.lower():
            result["reasoning"] = {"effort": "minimal"}
        elif "low" in model.lower():
            result["reasoning"] = {"effort": "low"}
        elif "medium" in model.lower():
            result["reasoning"] = {"effort": "medium"}
        elif "high" in model.lower():
            result["reasoning"] = {"effort": "high"}

    # Removed duplicate logging - it's already logged in handle_messages

    return result

def _map_anthropic_request_to_openai(body: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """
    Build OpenAI chat.completions payload from an Anthropic /v1/messages request body.
    """
    sys_msg = _anthropic_system_to_openai(body.get("system"))
    messages, tool_id_map = _anthropic_messages_to_openai(body.get("messages", []))
    if sys_msg:
        messages.insert(0, sys_msg)

    oai: Dict[str, Any] = {"messages": messages}

    # streaming
    if body.get("stream") is not None:
        oai["stream"] = bool(body["stream"])
    else:
        oai["stream"] = False

    # temperature / top_p
    if "temperature" in body:
        oai["temperature"] = body["temperature"]
    if "top_p" in body and body["top_p"] is not None:
        oai["top_p"] = body["top_p"]

    # stop sequences -> "stop"
    if "stop_sequences" in body and body["stop_sequences"]:
        stops = body["stop_sequences"]
        oai["stop"] = stops if isinstance(stops, list) else [stops]

    # max_tokens
    if isinstance(body.get("max_tokens"), int):
        oai["max_tokens"] = body["max_tokens"]

    # tools & tool_choice
    if body.get("tools"):
        oai["tools"] = _anthropic_tools_to_openai(body["tools"])
    if "tool_choice" in body:
        oai["tool_choice"] = _anthropic_tool_choice_to_openai(body["tool_choice"])

    # response_format (json mode)
    rf = body.get("response_format")
    if isinstance(rf, dict) and rf.get("type") == "json_object":
        oai["response_format"] = {"type": "json_object"}
    elif rf == "json":
        oai["response_format"] = {"type": "json_object"}

    return oai, tool_id_map

# =========================== Provider URL & Headers ==========================

def _build_upstream_url_and_headers(cfg: ModelConfig) -> Tuple[str, Dict[str, str]]:
    """
    Prepare the POST URL + headers to call the upstream provider.
    """
    provider = (cfg.provider or "openai").lower()

    if provider == "anthropic":
        # Native Anthropic API
        base = cfg.base_url.rstrip("/") if cfg.base_url else "https://api.anthropic.com"
        url = f"{base}/v1/messages"  # Anthropic uses /v1/messages

        # Check authentication method
        if cfg.auth_method == "oauth":
            # OAuth authentication (Bearer token with special headers)
            headers = {
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "Anthropic-Beta": "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14",
                "User-Agent": "claude-cli/1.0.83 (external, cli)",
                "X-App": "cli",
                "X-Stainless-Helper-Method": "stream",
                "X-Stainless-Lang": "js",
                "X-Stainless-Runtime": "node",
                "X-Stainless-Runtime-Version": "v24.3.0",
                "X-Stainless-Package-Version": "0.55.1",
                "Anthropic-Dangerous-Direct-Browser-Access": "true"
            }
            # Add ?beta=true to URL for OAuth
            url = f"{base}/v1/messages?beta=true"
        else:
            # Default to API key authentication (x-api-key header)
            headers = {
                "x-api-key": cfg.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            }

        headers.update(cfg.extra_headers or {})
        return url, headers

    elif provider == "azure":
        # Two patterns supported:
        # 1) Use explicit deployment/api-version fields (recommended).
        # 2) If base_url already contains full deployment path, fall back to {base}/chat/completions.
        if cfg.azure_deployment and cfg.azure_api_version:
            base = cfg.base_url.rstrip("/")
            url = f"{base}/openai/deployments/{cfg.azure_deployment}/chat/completions?api-version={cfg.azure_api_version}"
        else:
            base = cfg.base_url.rstrip("/")
            url = f"{base}/chat/completions"
        headers = {"api-key": cfg.api_key, "Content-Type": "application/json"}
        headers.update(cfg.extra_headers or {})
        return url, headers

    # OpenAI / OpenRouter / Ollama (OpenAI-compatible)
    if provider == "openai" and cfg.auth_method == "oauth":
        # Use ChatGPT backend API endpoint for OAuth (Codex implementation)
        url = "https://chatgpt.com/backend-api/codex/responses"
        headers = {
            "Version": "0.21.0",
            "Content-Type": "application/json",
            "Openai-Beta": "responses=experimental",
            "Session_id": str(uuid.uuid4()),
            "Accept": "text/event-stream",
            "Originator": "codex_cli_rs",
            "Authorization": f"Bearer {cfg.api_key}"
        }
        # Add ChatGPT Account ID if provided (use capital C)
        if cfg.extra_headers and cfg.extra_headers.get("Chatgpt-Account-Id"):
            headers["Chatgpt-Account-Id"] = cfg.extra_headers["Chatgpt-Account-Id"]
    else:
        # Standard OpenAI API headers
        base = cfg.base_url.rstrip("/")
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}

    headers.update(cfg.extra_headers or {})
    return url, headers

# =============================== SSE Utilities ===============================

def _sse_event(event_type: str, data_obj: Dict[str, Any]) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(data_obj, ensure_ascii=False)}\n\n".encode("utf-8")

def _new_message_stub(model_id: str) -> Dict[str, Any]:
    return {
        "type": "message",
        "id": f"msg_{uuid.uuid4().hex}",
        "role": "assistant",
        "model": model_id,
        "stop_reason": None,
        "stop_sequence": None,
    }

async def _iter_openai_sse(resp: ClientResponse):
    """
    Minimal SSE reader for OpenAI stream payload (lines with 'data: {...}').
    Yields parsed JSON dicts for each data line; skips keepalives and non-data.
    """
    buffer = b""
    async for chunk in resp.content.iter_any():
        if not chunk:
            continue
        buffer += chunk
        # Split by newline; keep trailing partial in buffer
        *lines, buffer = buffer.split(b"\n")
        for raw in lines:
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                return
            try:
                yield json.loads(data.decode("utf-8", errors="ignore"))
            except Exception:
                # ignore malformed
                continue

async def _iter_codex_sse(resp: ClientResponse):
    """
    SSE reader for Codex /backend-api/codex/responses.
    Yields (event_name, data) tuples for each SSE event.
    """
    buffer = b""
    async for chunk in resp.content.iter_any():
        if not chunk:
            continue
        buffer += chunk
        # Split by double newline (SSE event separator)
        while b"\n\n" in buffer:
            block, buffer = buffer.split(b"\n\n", 1)
            if not block:
                continue
            
            event_name = None
            event_data = None
            
            for line in block.split(b"\n"):
                line = line.strip()
                if line.startswith(b"event:"):
                    event_name = line[6:].strip().decode("utf-8", errors="ignore")
                elif line.startswith(b"data:"):
                    data_str = line[5:].strip()
                    if data_str and data_str != b"[DONE]":
                        try:
                            event_data = json.loads(data_str.decode("utf-8", errors="ignore"))
                        except Exception:
                            event_data = None
            
            if event_name and event_data is not None:
                yield event_name, event_data

async def _iter_anthropic_sse(resp: ClientResponse):
    """
    SSE reader for native Anthropic stream - just pass through the events.
    """
    buffer = b""
    async for chunk in resp.content.iter_any():
        if not chunk:
            continue
        buffer += chunk
        # Split by double newline (SSE event separator)
        while b"\n\n" in buffer:
            event, buffer = buffer.split(b"\n\n", 1)
            if event:
                yield event + b"\n\n"  # Include the separator for proper SSE format

# ================================ Handlers ===================================

async def handle_health(_req: web.Request) -> web.Response:
    return web.json_response({"ok": True})

async def handle_models(request: web.Request) -> web.Response:
    """
    Minimal model list; optionally could reflect the route's mapped models if authorized.
    """
    # If we want per-route reflection:
    # token = request.headers.get("Authorization","").replace("Bearer","").strip()
    # cfg = get_route(token)
    # ...
    return web.json_response({"data": [{"id": "claude-3-5-sonnet-latest", "type": "model"}]})

async def handle_messages(request: web.Request) -> web.StreamResponse:
    # --- extract route token from Authorization OR x-api-key ---
    auth = (request.headers.get("Authorization") or "").strip()
    x_api = (request.headers.get("x-api-key") or "").strip()

    token = ""
    if auth:
        parts = auth.split(None, 1)  # "Bearer <token>"
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
    if not token and x_api:
        token = x_api  # Anthropic CLI/SDK uses x-api-key

    if not token:
        return web.Response(status=401, text="missing Authorization or x-api-key")

    route = get_route(token)
    if route is None:
        src = "Authorization" if auth else ("x-api-key" if x_api else "none")
        return web.Response(status=401, text=f"unknown route token ({src})")

    # Parse request
    try:
        body = await request.json()
    except Exception:
        return web.json_response({
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "Invalid JSON body"}
        }, status=400)

    # Get the requested model and determine its size category
    requested_model = body.get("model", "")
    model_size = get_model_size(requested_model)

    # Select appropriate config based on model size
    if model_size == "small" and route.small:
        model_config = route.small
    elif model_size == "big" and route.big:
        model_config = route.big
    elif route.medium:
        model_config = route.medium
    else:
        # No config available for this model size
        return web.json_response({
            "type": "error",
            "error": {"type": "invalid_request_error",
                     "message": f"No provider configured for {model_size} models"}
        }, status=400)

    # Prepare upstream request based on provider
    is_anthropic = model_config.provider.lower() == "anthropic"
    tool_id_map = {}

    if is_anthropic:
        # Pass through native Anthropic format
        upstream_body = body.copy()
        upstream_body["model"] = model_config.model
    elif model_config.provider == "openai" and model_config.auth_method == "oauth":
        # Use ChatGPT backend API format for OAuth
        upstream_body = _map_anthropic_to_chatgpt_backend(body, model_config.model)
        # Note: ChatGPT backend doesn't use tool_id_map
    else:
        # Convert to OpenAI format for standard OpenAI-compatible providers
        upstream_body, tool_id_map = _map_anthropic_request_to_openai(body)
        upstream_body["model"] = model_config.model

    # Upstream URL + headers
    url, headers = _build_upstream_url_and_headers(model_config)
    timeout = ClientTimeout(total=float(os.getenv("REQUEST_TIMEOUT", "120")))
    
    # Log the full request being sent upstream
    print(f"\n📤 UPSTREAM REQUEST:")
    print(f"   Provider: {model_config.provider}")
    print(f"   Model: {model_config.model}")
    print(f"   Auth Method: {getattr(model_config, 'auth_method', 'api_key')}")
    print(f"   URL: {url}")
    print(f"   Headers: {json.dumps({k: _mask_secret(v) if 'key' in k.lower() or 'authorization' in k.lower() else v for k, v in headers.items()}, indent=2)}")
    print(f"   Request Body (size: {len(json.dumps(upstream_body))}):\n{json.dumps(upstream_body, indent=2)}")

    # streaming?
    do_stream = bool(body.get("stream", False))

    async with ClientSession(timeout=timeout) as sess:
        try:
            if do_stream:
                # set up SSE response to Claude client
                resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
                await resp.prepare(request)

                msg_stub = _new_message_stub(requested_model or "claude-3-5-sonnet-latest")
                await resp.write(_sse_event("message_start", {"message": msg_stub}))

                tool_calls_accum: List[Dict[str, Any]] = []
                text_started = False
                finish_reason: Optional[str] = None

                async with sess.post(url, json=upstream_body, headers=headers) as r:
                    print(f"\n📥 UPSTREAM RESPONSE (streaming):")
                    print(f"   Status: {r.status}")
                    print(f"   Headers: {dict(r.headers)}")
                    
                    # Handle upstream non-200 with structured error
                    if r.status >= 400:
                        try:
                            errj = await r.json()
                        except Exception:
                            errj = {"message": await r.text()}

                        # Log upstream errors for debugging
                        print(f"❌ UPSTREAM ERROR {r.status}:")
                        print(f"   Model: {model_config.model}")
                        print(f"   Provider: {model_config.provider}")
                        print(f"   URL: {url}")
                        print(f"   Auth Method: {getattr(model_config, 'auth_method', 'default')}")
                        print(f"   Error response: {json.dumps(errj, indent=2)[:1000]}")

                        # Parse error details - Codex may have nested error structure
                        error_msg = errj.get("message", str(errj))
                        error_type = "api_error"
                        
                        # Try to extract nested error details if present (Codex format)
                        if isinstance(error_msg, str) and error_msg.startswith("{"):
                            try:
                                nested_error = json.loads(error_msg)
                                if "error" in nested_error:
                                    error_details = nested_error["error"]
                                    error_msg = error_details.get("message", error_msg)
                                    error_type = error_details.get("type", "api_error")
                            except Exception:
                                pass  # Keep original error_msg if parsing fails

                        # emit an SSE-style error event in Anthropic format
                        await resp.write(_sse_event("error", {
                            "type": "error",
                            "error": {"type": error_type, "message": error_msg}
                        }))
                        await resp.write(_sse_event("message_stop", {}))
                        await resp.write_eof()
                        return resp

                    if is_anthropic:
                        # Pass through native Anthropic SSE events
                        async for event in _iter_anthropic_sse(r):
                            try:
                                await resp.write(event)
                            except (ConnectionResetError, ClientConnectionResetError) as e:
                                # Client disconnected, stop streaming
                                print(f"⚠️ Client disconnected during streaming: {e}")
                                break
                    elif model_config.provider == "openai" and getattr(model_config, 'auth_method', None) == "oauth":
                        # Convert Codex SSE to Anthropic format for streaming
                        tool_calls_state = {}  # Track ongoing tool calls
                        
                        async for event_name, event_data in _iter_codex_sse(r):
                            try:
                                # Handle text output events
                                if event_name == "response.output_text.delta":
                                    text = event_data.get("delta", "")
                                    if text:
                                        if not text_started:
                                            await resp.write(_sse_event("content_block_start", {"index": 0, "type": "text"}))
                                            text_started = True
                                        await resp.write(_sse_event("content_block_delta", {
                                            "index": 0, "delta": {"type": "text_delta", "text": text}
                                        }))
                                
                                # Handle function call events
                                elif event_name == "response.function_call.arguments.delta":
                                    call_id = event_data.get("call_id", "default")
                                    if call_id not in tool_calls_state:
                                        # Start a new tool use block
                                        content_index = 1 if text_started else 0
                                        tool_calls_state[call_id] = {
                                            "index": content_index,
                                            "id": call_id,
                                            "name": event_data.get("name", "function"),
                                            "arguments": ""
                                        }
                                        await resp.write(_sse_event("content_block_start", {
                                            "index": content_index,
                                            "type": "tool_use",
                                            "id": call_id,
                                            "name": tool_calls_state[call_id]["name"]
                                        }))
                                    
                                    # Stream the arguments delta
                                    args_delta = event_data.get("delta", "")
                                    if args_delta:
                                        tool_calls_state[call_id]["arguments"] += args_delta
                                        await resp.write(_sse_event("content_block_delta", {
                                            "index": tool_calls_state[call_id]["index"],
                                            "delta": {"type": "input_json_delta", "partial_json": args_delta}
                                        }))
                                
                                elif event_name == "response.function_call.completed":
                                    # Complete the tool use block
                                    call_id = event_data.get("call_id", "default")
                                    if call_id in tool_calls_state:
                                        await resp.write(_sse_event("content_block_stop", {
                                            "index": tool_calls_state[call_id]["index"]
                                        }))
                                
                                elif event_name == "response.completed":
                                    # Response is complete, set finish reason
                                    if event_data.get("finish_reason") == "max_tokens":
                                        finish_reason = "length"
                                    elif event_data.get("finish_reason") == "tool_calls":
                                        finish_reason = "tool_calls"
                                    else:
                                        finish_reason = "stop"
                                        
                            except (ConnectionResetError, ClientConnectionResetError) as e:
                                print(f"⚠️ Client disconnected during Codex streaming: {e}")
                                break
                        
                        # Close any open text block
                        if text_started:
                            try:
                                await resp.write(_sse_event("content_block_stop", {"index": 0}))
                            except (ConnectionResetError, ClientConnectionResetError) as e:
                                print(f"⚠️ Client disconnected during Codex finalization: {e}")
                                return resp
                        
                        # Send final message_stop event for Codex
                        try:
                            await resp.write(_sse_event("message_stop", {}))
                        except (ConnectionResetError, ClientConnectionResetError) as e:
                            print(f"⚠️ Client disconnected during Codex final stop: {e}")
                            pass
                    
                    else:
                        # Convert regular OpenAI SSE to Anthropic format
                        async for chunk in _iter_openai_sse(r):
                            choice = (chunk.get("choices") or [{}])[0]
                            delta = choice.get("delta") or {}
                            finish_reason = finish_reason or choice.get("finish_reason")

                            # Text deltas
                            txt = delta.get("content") or delta.get("text")
                            if txt:
                                try:
                                    if not text_started:
                                        await resp.write(_sse_event("content_block_start", {"index": 0, "type": "text"}))
                                        text_started = True
                                    await resp.write(_sse_event("content_block_delta", {
                                        "index": 0, "delta": {"type": "text_delta", "text": txt}
                                    }))
                                except (ConnectionResetError, ClientConnectionResetError) as e:
                                    # Client disconnected, stop streaming
                                    print(f"⚠️ Client disconnected during OpenAI streaming: {e}")
                                    break

                            # Tool calls deltas
                            tcd = delta.get("tool_calls")
                            if isinstance(tcd, list):
                                for tc in tcd:
                                    idx = tc.get("index", 0)
                                    while len(tool_calls_accum) <= idx:
                                        tool_calls_accum.append({"id": None, "name": None, "arguments": ""})
                                    entry = tool_calls_accum[idx]
                                    if tc.get("id"):
                                        entry["id"] = tc["id"]
                                    fn = tc.get("function") or {}
                                    if fn.get("name"):
                                        entry["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        entry["arguments"] += fn["arguments"]

                        # Close text block if opened (only for OpenAI conversion)
                        if text_started:
                            try:
                                await resp.write(_sse_event("content_block_stop", {"index": 0}))
                            except (ConnectionResetError, ClientConnectionResetError) as e:
                                print(f"⚠️ Client disconnected during OpenAI streaming finalization: {e}")
                                return resp

                        # Emit finalized tool_use blocks (only for OpenAI conversion)
                        content_index = 1 if text_started else 0
                        for tc in tool_calls_accum:
                            args_json = {}
                            if tc["arguments"]:
                                try:
                                    args_json = json.loads(tc["arguments"])
                                except Exception:
                                    args_json = {"_raw": tc["arguments"]}
                            tool_id = tc["id"] or f"tool_{uuid.uuid4().hex[:8]}"
                            tool_name = tc["name"] or "function"
                            try:
                                await resp.write(_sse_event("content_block_start", {
                                    "index": content_index,
                                    "type": "tool_use",
                                    "id": tool_id,
                                    "name": tool_name,
                                    "input": args_json
                                }))
                                await resp.write(_sse_event("content_block_stop", {"index": content_index}))
                            except (ConnectionResetError, ClientConnectionResetError) as e:
                                print(f"⚠️ Client disconnected during tool emission: {e}")
                                return resp
                            content_index += 1

                        # Final stop (only for OpenAI conversion)
                        try:
                            await resp.write(_sse_event("message_stop", {}))
                        except (ConnectionResetError, ClientConnectionResetError) as e:
                            print(f"⚠️ Client disconnected during final stop: {e}")
                            pass  # Already at the end, just return
                    
                    print(f"✅ Streaming response completed successfully")
                return resp

            else:
                # Non-streaming flow
                async with sess.post(url, json=upstream_body, headers=headers) as r:
                    if r.status >= 400:
                        try:
                            errj = await r.json()
                        except Exception:
                            errj = {"message": await r.text()}

                        # Log upstream errors for debugging
                        print(f"❌ UPSTREAM ERROR {r.status}:")
                        print(f"   Model: {model_config.model}")
                        print(f"   Provider: {model_config.provider}")
                        print(f"   URL: {url}")
                        print(f"   Auth Method: {getattr(model_config, 'auth_method', 'default')}")
                        print(f"   Error response: {json.dumps(errj, indent=2)[:1000]}")
                        
                        # Parse error details - Codex may have nested error structure
                        error_msg = errj.get("message", str(errj))
                        error_type = "api_error"
                        
                        # Try to extract nested error details if present (Codex format)
                        if isinstance(error_msg, str) and error_msg.startswith("{"):
                            try:
                                nested_error = json.loads(error_msg)
                                if "error" in nested_error:
                                    error_details = nested_error["error"]
                                    error_msg = error_details.get("message", error_msg)
                                    error_type = error_details.get("type", "api_error")
                            except Exception:
                                pass  # Keep original error_msg if parsing fails
                        
                        # Return error in Anthropic format
                        return web.json_response({
                            "type": "error",
                            "error": {"type": error_type, "message": error_msg}
                        }, status=502)

                    # Check if this is OAuth/Codex (which always returns SSE, never JSON)
                    if model_config.provider == "openai" and getattr(model_config, 'auth_method', None) == "oauth":
                        # Buffer Codex SSE events for non-streaming response
                        accumulated_text = ""
                        tool_calls = {}  # Track tool calls by their ID
                        stop_reason = "end_turn"
                        
                        async for event_name, event_data in _iter_codex_sse(r):
                            # Handle text output events
                            if event_name == "response.output_text.delta":
                                text = event_data.get("delta", "")
                                if text:
                                    accumulated_text += text
                            
                            # Handle function call events
                            elif event_name == "response.function_call.arguments.delta":
                                # Accumulate function arguments
                                call_id = event_data.get("call_id", "default")
                                if call_id not in tool_calls:
                                    tool_calls[call_id] = {
                                        "id": call_id,
                                        "name": event_data.get("name", "function"),
                                        "arguments": ""
                                    }
                                args_delta = event_data.get("delta", "")
                                if args_delta:
                                    tool_calls[call_id]["arguments"] += args_delta
                            
                            elif event_name == "response.function_call.completed":
                                # Mark function call as complete
                                call_id = event_data.get("call_id", "default")
                                if call_id in tool_calls:
                                    tool_calls[call_id]["completed"] = True
                            
                            elif event_name == "response.completed":
                                # Response is complete
                                if event_data.get("finish_reason") == "max_tokens":
                                    stop_reason = "max_tokens"
                                elif event_data.get("finish_reason") == "tool_calls":
                                    stop_reason = "tool_use"
                        
                        # Build Anthropic response from accumulated data
                        content = []
                        
                        # Always add text content, even if empty (Claude Code expects it)
                        content.append({"type": "text", "text": accumulated_text or ""})
                        
                        # Add tool calls
                        for tc in tool_calls.values():
                            if tc.get("name"):
                                args_json = {}
                                if tc.get("arguments"):
                                    try:
                                        args_json = json.loads(tc["arguments"])
                                    except Exception:
                                        args_json = {"_raw": tc["arguments"]}
                                
                                content.append({
                                    "type": "tool_use",
                                    "id": tc["id"],
                                    "name": tc["name"],
                                    "input": args_json
                                })
                        
                        response = {
                            "id": f"msg_{uuid.uuid4().hex[:8]}",
                            "type": "message",
                            "role": "assistant",
                            "content": content,
                            "stop_reason": stop_reason,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0}
                        }
                        
                        print(f"\n📥 UPSTREAM RESPONSE (non-streaming OAuth/Codex SSE):")
                        print(f"   Status: {r.status}")
                        print(f"   Buffered Codex SSE into Anthropic format")
                        print(f"   Text length: {len(accumulated_text)}")
                        print(f"   Tool calls: {len(tool_calls)}")
                        
                        return web.json_response(response)
                    
                    else:
                        # Regular JSON response for other providers
                        j = await r.json()
                        
                        # Log the full response
                        print(f"\n📥 UPSTREAM RESPONSE (non-streaming):")
                        print(f"   Status: {r.status}")
                        print(f"   Response Body (size: {len(json.dumps(j))}):\n{json.dumps(j, indent=2)}")

                        if is_anthropic:
                            # Pass through native Anthropic response
                            return web.json_response(j)

                        # Convert OpenAI response to Anthropic format
                        choice = (j.get("choices") or [{}])[0]
                        msg = choice.get("message") or {}
                        finish = choice.get("finish_reason")

                    # Text
                    text_content = msg.get("content")
                    if isinstance(text_content, list):
                        # Some providers might return list; collapse to text
                        merged = "".join(part.get("text", "") if isinstance(part, dict) else str(part)
                                         for part in text_content)
                        text = merged
                    else:
                        text = text_content or ""

                    content_blocks: List[Dict[str, Any]] = []
                    if text:
                        content_blocks.append({"type": "text", "text": text})

                    # Tool calls (non-stream)
                    tool_calls = msg.get("tool_calls") or []
                    for tc in tool_calls:
                        tool_id = tc.get("id") or f"tool_{uuid.uuid4().hex[:8]}"
                        fn = tc.get("function") or {}
                        tool_name = fn.get("name") or "function"
                        args = {}
                        if fn.get("arguments"):
                            try:
                                args = json.loads(fn["arguments"])
                            except Exception:
                                args = {"_raw": fn["arguments"]}
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tool_id,
                            "name": tool_name,
                            "input": args
                        })

                    # Usage mapping
                    usage = j.get("usage") or {}
                    anthropic_usage = {
                        "input_tokens": usage.get("prompt_tokens"),
                        "output_tokens": usage.get("completion_tokens"),
                        "total_tokens": usage.get("total_tokens"),
                    }

                    # stop_reason mapping (best-effort)
                    if finish == "tool_calls":
                        stop_reason = "tool_use"
                    elif finish == "stop":
                        stop_reason = "end_turn"
                    else:
                        stop_reason = finish

                    return web.json_response({
                        "id": f"msg_{uuid.uuid4().hex}",
                        "type": "message",
                        "role": "assistant",
                        "model": requested_model or "claude-3-5-sonnet-latest",
                        "content": content_blocks,
                        "stop_reason": stop_reason,
                        "usage": anthropic_usage,
                    })

        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Log the full error details
            import traceback
            error_details = traceback.format_exc()
            print(f"❌ PROXY ERROR 502: {str(e)}")
            print(f"   Model: {model_config.model}")
            print(f"   Provider: {model_config.provider}")
            print(f"   URL: {url}")
            print(f"   Auth Method: {getattr(model_config, 'auth_method', 'default')}")
            print(f"   Full traceback:\n{error_details}")

            # Map to Anthropic error shape
            return web.json_response({
                "type": "error",
                "error": {"type": "api_error", "message": f"Upstream error: {str(e)}"}
            }, status=502)

# ============================== App Bootstrap ================================

def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/messages", handle_messages)
    return app

async def start_proxy(host: str = "127.0.0.1", port: int = 8082):
    """
    Start the proxy server (call once from your broker).
    """
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner

if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8082"))
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_proxy(host, port))
    print(f"[proxy] listening on http://{host}:{port}")
    loop.run_forever()
