"""
Claude Code session JSONL parser.
Reads a Claude Code session log and extracts generations, tool uses,
prompts, and metadata into a structured, typed dictionary.
"""

import json
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from posthog_llma.discovery.engine import (
    GitHubDiscoveryError,
    discover_repository,
)

# --- 1. TOOL DISPATCH REGISTRY ---

GITHUB_DISCOVERY_SKILL_ID = "Github-Discovery"

def handle_github_discovery_tool(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Run headless GitHub tree discovery from agent tool input."""
    if not isinstance(tool_input, dict):
        return {"ok": False, "error": "tool_input must be an object"}

    url = tool_input.get("url") or tool_input.get("repo")
    if not url or not isinstance(url, str):
        return {"ok": False, "error": "Missing string field 'url' (or 'repo')."}

    try:
        # Extract and sanitize inputs
        ref = str(tool_input["ref"]) if tool_input.get("ref") else None
        prefix = str(tool_input["path_prefix"]) if tool_input.get("path_prefix") else None
        token = str(tool_input["token"]) if tool_input.get("token") else None
        
        # Enforce reasonable defaults
        max_nodes = int(tool_input.get("max_tree_nodes", 50_000))
        
        out_f = tool_input.get("output_file")
        out_path = out_f.strip() if isinstance(out_f, str) and out_f.strip() else None

        md_f = tool_input.get("markdown_file")
        md_path = md_f if isinstance(md_f, str) else None

        tree = discover_repository(
            url,
            ref=ref,
            path_prefix=prefix,
            max_tree_nodes=max_nodes,
            token=token,
            output_file=out_path,
            markdown_file=md_path,
        )
        return {"ok": True, "map": tree}

    except GitHubDiscoveryError as e:
        return {"ok": False, "error": str(e)}
    except (TypeError, ValueError) as e:
        return {"ok": False, "error": f"Invalid parameters: {e}"}


SKILL_TOOL_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    GITHUB_DISCOVERY_SKILL_ID: handle_github_discovery_tool,
}

def invoke_skill_tool_handler(skill_id: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke a registered skill handler by id, or return a structured error."""
    handler = SKILL_TOOL_HANDLERS.get(skill_id)
    if not handler:
        return {"ok": False, "error": f"No handler registered for skill {skill_id!r}"}
    return handler(tool_input)


# --- 2. CLAUDE LOG PARSER ---

def find_session_log(session_id: str, cwd: str) -> Optional[str]:
    """Find the JSONL session log file in ~/.claude/projects/."""
    project_dir_name = cwd.replace("/", "-")
    path = Path.home() / ".claude" / "projects" / project_dir_name / f"{session_id}.jsonl"
    return str(path) if path.is_file() else None


def _parse_jsonl(filepath: str) -> List[Dict[str, Any]]:
    """Reads JSONL into memory once to avoid double disk I/O."""
    lines = []
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return lines


def parse_session(jsonl_path: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a Claude Code session JSONL file into structured data."""
    entries = _parse_jsonl(jsonl_path)
    
    generations_by_msg_id: Dict[str, Tuple[Dict, List]] = {}
    generations_order: List[str] = []
    tool_results: Dict[str, Dict] = {}
    prompts: List[Dict[str, Any]] = []
    metadata: Dict[str, Any] = {}

    session_id = ""
    privacy_mode = bool(config.get("privacy_mode", False))

    uuid_to_prompt_id: Dict[str, str] = {}
    uuid_to_parent: Dict[str, str] = {}

    # Pass 1 (In-Memory): Build UUID maps for tracing prompt origins
    for entry in entries:
        uid = entry.get("uuid")
        if uid:
            if parent := entry.get("parentUuid"):
                uuid_to_parent[uid] = parent
            if prompt_id := entry.get("promptId"):
                uuid_to_prompt_id[uid] = prompt_id

    def resolve_prompt_id(entry_uuid: str) -> str:
        """Walk parentUuid chain (up to 30 levels) to find the originating promptId."""
        current = entry_uuid
        for _ in range(30):
            if current in uuid_to_prompt_id:
                return uuid_to_prompt_id[current]
            current = uuid_to_parent.get(current)
            if not current:
                break
        return ""

    # Pass 2 (In-Memory): Extract Data
    for entry in entries:
        entry_type = entry.get("type", "")

        if entry_type == "permission-mode":
            session_id = entry.get("sessionId", "")

        elif entry_type == "user":
            _process_user_entry(entry, privacy_mode, tool_results, prompts, uuid_to_prompt_id)

        elif entry_type == "assistant":
            _process_assistant_entry(entry, resolve_prompt_id, generations_by_msg_id, generations_order)

        elif entry_type == "system" and entry.get("subtype") == "turn_duration":
            metadata["duration_ms"] = entry.get("durationMs")
            metadata["message_count"] = entry.get("messageCount")
            if not session_id:
                session_id = entry.get("sessionId", "")

        # Capture global metadata
        for key in ["version", "cwd", "gitBranch"]:
            if val := entry.get(key):
                norm_key = "git_branch" if key == "gitBranch" else key
                if norm_key not in metadata:
                    metadata[norm_key] = val

    # Flatten deduplicated generations and tool uses
    generations, tool_uses = [], []
    for key in generations_order:
        gen, tus = generations_by_msg_id[key]
        generations.append(gen)
        
        # Attach tool results before appending
        for tu in tus:
            if result := tool_results.get(tu["tool_use_id"]):
                tu["result"] = result
        tool_uses.extend(tus)

    return {
        "session_id": session_id,
        "generations": generations,
        "tool_uses": tool_uses,
        "prompts": prompts,
        "metadata": metadata,
    }


def _process_user_entry(
    entry: Dict[str, Any], privacy_mode: bool, 
    tool_results: Dict[str, Any], prompts: List[Dict], uuid_to_prompt_id: Dict[str, str]
) -> None:
    msg = entry.get("message", {})
    if msg.get("role") != "user":
        return

    prompt_id = entry.get("promptId", "")
    timestamp = entry.get("timestamp", "")
    has_tool_result = False

    # Extract Tool Results
    if tool_result_top := entry.get("toolUseResult"):
        if source_tool_id := entry.get("sourceToolUseID"):
            tool_results[source_tool_id] = tool_result_top
            has_tool_result = True

    msg_content = msg.get("content", "")
    if isinstance(msg_content, list):
        for item in msg_content:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                if tool_use_id := item.get("tool_use_id"):
                    tool_results[tool_use_id] = item
                    has_tool_result = True

    if has_tool_result or entry.get("isMeta"):
        return

    # Extract Prompt Content
    content = msg_content
    if isinstance(content, list):
        text_parts = [i.get("text", "") for i in content if isinstance(i, dict) and i.get("type") == "text"]
        content = "\n".join(text_parts)

    if not isinstance(content, str) or not content.strip():
        return

    # Create mapping and save prompt
    effective_prompt_id = prompt_id or entry.get("uuid", f"unknown-prompt-{uuid.uuid4().hex[:8]}")
    if entry_uuid := entry.get("uuid"):
        uuid_to_prompt_id[entry_uuid] = effective_prompt_id

    prompts.append({
        "prompt_id": effective_prompt_id,
        "timestamp": timestamp,
        "text": None if privacy_mode else content,
    })


def _process_assistant_entry(
    entry: Dict[str, Any], resolve_prompt_id: Callable[[str], str], 
    generations_by_msg_id: Dict[str, Tuple[Dict, List]], generations_order: List[str]
) -> None:
    msg = entry.get("message", {})
    if msg.get("role") != "assistant":
        return

    msg_id = msg.get("id", "")
    entry_uuid = entry.get("uuid", "")
    prompt_id = resolve_prompt_id(entry_uuid)
    span_id = str(uuid.uuid4())
    timestamp = entry.get("timestamp", "")

    text_parts, entry_tool_uses = [], []

    # Parse message content block
    if isinstance(msg.get("content"), list):
        for item in msg.get("content"):
            if not isinstance(item, dict): continue
            
            i_type = item.get("type")
            if i_type == "text":
                text_parts.append(item.get("text", ""))
            elif i_type == "thinking" and item.get("thinking"):
                text_parts.append(item.get("thinking", ""))
            elif i_type == "tool_use":
                entry_tool_uses.append({
                    "tool_use_id": item.get("id", ""),
                    "name": item.get("name", "unknown"),
                    "input": item.get("input"),
                    "generation_span_id": span_id,
                    "prompt_id": prompt_id,
                    "timestamp": timestamp,
                })

    usage = msg.get("usage", {})
    generation = {
        "model": msg.get("model", "unknown"),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
        "stop_reason": msg.get("stop_reason"),
        "timestamp": timestamp,
        "prompt_id": prompt_id,
        "span_id": span_id,
        "output_text": "\n".join(text_parts) if text_parts else None,
        "tool_use_blocks": [{"type": "tool_use", "name": tu["name"], "input": tu.get("input")} for tu in entry_tool_uses],
        "is_error": msg.get("stop_reason") == "error",
        "error_message": msg.get("error_message"),
    }

    # Deduplicate via overwrite (streaming logs have partial updates first)
    key = msg_id or entry_uuid or f"gen-{span_id}"
    if key not in generations_by_msg_id:
        generations_order.append(key)
    generations_by_msg_id[key] = (generation, entry_tool_uses)