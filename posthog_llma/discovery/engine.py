"""
Headless GitHub repo discovery engine.
Optimized for AI agents: Iterative tree mapping, concurrent API calls, 
and generic tech-stack heuristics.

Output shape aligns with Integration Registry-style trees:
  { name, path, type: tree|blob, children: {...}, descendantFiles, descendantFolders }

Uses the GitHub REST API (git trees + refs). Set GITHUB_TOKEN for higher rate limits.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

GITHUB_API = "https://api.github.com"


class GitHubDiscoveryError(Exception):
    """Raised for unrecoverable API, parse, or limit errors."""


@dataclass(frozen=True)
class ParsedRepoRef:
    owner: str
    repo: str
    ref: Optional[str] = None
    path_prefix: Optional[str] = None


# --- 1. PARSING & FILTERING ---

def parse_github_url(url: str) -> ParsedRepoRef:
    """Parse owner/repo and optional ref + path from a GitHub URL or shorthand."""
    raw = (url or "").strip()
    if not raw:
        raise GitHubDiscoveryError("Empty URL or repo string.")

    if raw.startswith("git@github.com:"):
        raw = "https://github.com/" + raw.removeprefix("git@github.com:").removesuffix(".git")
    raw = raw.removesuffix(".git")

    if re.fullmatch(r"[\w.-]+/[\w.-]+", raw):
        o, r = raw.split("/", 1)
        return ParsedRepoRef(owner=o, repo=r)

    m = re.match(
        r"^https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)"
        r"(?:/(?:tree|blob)/(?P<ref>[^/]+)(?:/(?P<path>.+))?)?/?$",
        raw,
    )
    if not m:
        raise GitHubDiscoveryError(f"Could not parse GitHub URL: {url!r}.")
        
    return ParsedRepoRef(
        owner=m.group("owner"), repo=m.group("repo"), 
        ref=m.group("ref"), path_prefix=m.group("path")
    )


IGNORED_DIR_SEGMENTS: frozenset[str] = frozenset({
    "node_modules", ".git", "__pycache__", "dist", "build", 
    ".next", ".nuxt", "coverage", ".venv", "venv", "target"
})

_NOISE_FILE_BASENAMES_LOWER: frozenset[str] = frozenset({
    ".ds_store", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", 
    "poetry.lock", "pipfile.lock", "composer.lock", "gemfile.lock", 
    "go.sum", "cargo.lock", "bun.lockb", "npm-shrinkwrap.json"
})


def filter_discovery_noise(blobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop blobs under ignored directories and common lock files."""
    filtered = []
    for b in blobs:
        path = b.get("path") or ""
        parts = path.split("/")
        if any(seg in IGNORED_DIR_SEGMENTS for seg in parts[:-1]):
            continue
        if parts and parts[-1].lower() in _NOISE_FILE_BASENAMES_LOWER:
            continue
        filtered.append(b)
    return filtered


def _filter_by_prefix(blobs: list[dict[str, Any]], prefix: Optional[str]) -> list[dict[str, Any]]:
    if not prefix or not prefix.strip().strip("/"):
        return blobs
    p = prefix.strip().strip("/")
    plen = len(p) + 1
    
    out = []
    for b in blobs:
        path = b["path"]
        if path.startswith(p + "/"):
            out.append({**b, "path": path[plen:]})
        elif path == p and b.get("type") == "blob":
            out.append(b)
    return out


# --- 2. HEURISTICS & STATS ---

_ROOT_FILE_STACK: tuple[tuple[str, str], ...] = (
    ("package.json", "nodejs"), ("pnpm-workspace.yaml", "pnpm-workspace"), 
    ("tsconfig.json", "typescript"), ("requirements.txt", "python"), 
    ("pyproject.toml", "python"), ("cargo.toml", "rust"), ("go.mod", "go"), 
    ("gemfile", "ruby"), ("pom.xml", "jvm-maven"), ("build.gradle", "jvm-gradle"), 
    ("dockerfile", "docker"), ("docker-compose.yml", "compose"), 
    ("next.config.js", "nextjs"), ("vite.config.ts", "vite")
)

def detect_tech_stack(blobs: list[dict[str, Any]]) -> list[str]:
    root_lower = {b.get("path", "").lower() for b in blobs if "/" not in b.get("path", "")}
    out, seen = [], set()
    for fname, tag in _ROOT_FILE_STACK:
        if fname in root_lower and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def _importance_score(path: str) -> int:
    pl, base = path.lower(), path.rsplit("/", 1)[-1].lower()
    if base in ("readme.md", "readme.rst", "readme.txt"): return 100
    if pl.endswith("internal/registry.json"): return 98
    if base == "registry.json": return 95
    if base in ("package.json", "pyproject.toml", "cargo.toml", "go.mod"): return 88
    if base in ("main.py", "app.py", "index.ts", "main.ts"): return 82
    if base in ("dockerfile", "makefile"): return 72
    return 65 if base.endswith(".md") and "/" not in path else 0


def suggest_integration_roots(tree: dict[str, Any]) -> dict[str, Any]:
    hints: dict[str, Any] = {"likely_provider_roots": [], "registry_json_paths": []}
    stack = [(tree, "")]
    
    while stack:
        node, _ = stack.pop()
        name, path, typ = node.get("name", ""), node.get("path", ""), node.get("type", "")
        
        if typ != "tree":
            if name.lower() == "registry.json" or path.endswith("Internal/Registry.json"):
                hints["registry_json_paths"].append(path)
            continue
            
        rel = path or name
        if name in ("Providers", "Provider", "integrations", "Integrations"):
            hints["likely_provider_roots"].append(rel)
            
        for child in (node.get("children") or {}).values():
            stack.append((child, ""))
            
    if "Providers" in (tree.get("children") or {}) and "Providers" not in hints["likely_provider_roots"]:
        hints["likely_provider_roots"].append("Providers")
    return hints


# --- 3. ITERATIVE TREE BUILDER ---

def build_tree_from_paths(blobs: list[dict[str, Any]], *, root_name: str = "root") -> dict[str, Any]:
    """Iterative tree builder: safe for extreme depths (no RecursionError)."""
    root = {
        "name": root_name, "path": "", "type": "tree", 
        "children": {}, "descendantFiles": 0, "descendantFolders": 0
    }

    for b in sorted(blobs, key=lambda r: r["path"]):
        parts = b["path"].split("/")
        curr = root
        for i, seg in enumerate(parts):
            if seg not in curr["children"]:
                if i == len(parts) - 1:
                    curr["children"][seg] = {
                        "name": seg, "path": b["path"], "type": "blob", 
                        "children": {}, "descendantFiles": 0, "descendantFolders": 0,
                        "sha": b.get("sha", "")
                    }
                    if "size" in b and b["size"] is not None:
                        curr["children"][seg]["size"] = b["size"]
                else:
                    curr["children"][seg] = {
                        "name": seg, "path": "/".join(parts[:i+1]), "type": "tree", 
                        "children": {}, "descendantFiles": 0, "descendantFolders": 0
                    }
            curr = curr["children"][seg]

    # Iterative Post-Order Traversal for descendants
    stack, order = [root], []
    while stack:
        node = stack.pop()
        order.append(node)
        for child in node["children"].values():
            if child["type"] == "tree":
                stack.append(child)

    for node in reversed(order):
        d_files, d_folders = 0, 0
        for child in node["children"].values():
            if child["type"] == "blob":
                d_files += 1
            else:
                d_files += child["descendantFiles"]
                d_folders += 1 + child["descendantFolders"]
        node["descendantFiles"] = d_files
        node["descendantFolders"] = d_folders

    return root


# --- 4. GITHUB API & CONCURRENCY ---

async def _github_request(client: Any, method: str, path: str, *, headers: dict[str, str], params: Optional[dict] = None) -> Any:
    r = await client.request(method, f"{GITHUB_API}{path}", headers=headers, params=params, timeout=60.0)
    if r.status_code == 403:
        raise GitHubDiscoveryError("GitHub API returned 403. Set GITHUB_TOKEN in the environment.")
    if r.status_code != 200:
        raise GitHubDiscoveryError(f"GitHub API {r.status_code} for {path}: {r.text[:200]}")
    return r.json()


async def _fetch_blob_text_preview(client: Any, owner: str, repo: str, sha: str, headers: dict[str, str], max_chars: int = 500) -> str:
    if not sha: return ""
    try:
        data = await _github_request(client, "GET", f"/repos/{owner}/{repo}/git/blobs/{sha}", headers=headers)
        raw, enc = data.get("content"), data.get("encoding")
        if enc == "base64":
            return base64.b64decode(raw).decode("utf-8", errors="replace")[:max_chars]
        return str(raw)[:max_chars]
    except Exception:
        return ""


async def collect_smart_summaries(client: Any, owner: str, repo: str, blobs: list[dict[str, Any]], headers: dict[str, str], *, limit: int = 5) -> list[dict[str, Any]]:
    """Concurrent smart summaries to eliminate latency."""
    scored = [(sc, b["path"], b["sha"]) for b in blobs if (sc := _importance_score(b.get("path", ""))) > 0]
    scored = sorted(scored, key=lambda x: (-x[0], x[1]))[:limit]
    
    sem = asyncio.Semaphore(5)  # Limit concurrent blob fetches
    async def fetch_summary(sc, path, sha):
        async with sem:
            preview = await _fetch_blob_text_preview(client, owner, repo, sha, headers, max_chars=500)
            return {"path": path, "sha": sha, "content_preview": preview, "importance_score": sc}

    tasks = [fetch_summary(sc, p, s) for sc, p, s in scored]
    return await asyncio.gather(*tasks)


async def _fetch_tree_bfs_flat(client: Any, owner: str, repo: str, root_tree_sha: str, headers: dict[str, str], max_tree_nodes: int) -> list[dict[str, Any]]:
    """Batched BFS tree walk respecting concurrency limits."""
    blobs, queue = [], [(root_tree_sha, "")]
    seen_trees = 0
    sem = asyncio.Semaphore(10)

    async def fetch_dir(sha, prefix):
        async with sem:
            return await _github_request(client, "GET", f"/repos/{owner}/{repo}/git/trees/{sha}", headers=headers)

    while queue and seen_trees < max_tree_nodes:
        batch, queue = queue[:10], queue[10:]
        seen_trees += len(batch)
        
        tasks = [fetch_dir(sha, prefix) for sha, prefix in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for (sha, prefix), data in zip(batch, results):
            if isinstance(data, Exception): continue
            for item in data.get("tree", []):
                full = f"{prefix}/{item['path']}" if prefix else item["path"]
                if item["type"] == "blob":
                    blobs.append({"path": full, "sha": item["sha"], "size": item.get("size")})
                elif item["type"] == "tree":
                    queue.append((item["sha"], full))
                    
    if seen_trees >= max_tree_nodes:
        raise GitHubDiscoveryError(f"Exceeded max_tree_nodes={max_tree_nodes}.")
    return blobs


# --- 5. EXPORT UTILS ---

def render_navigation_markdown(tree: dict[str, Any]) -> str:
    meta = tree.get("repo") or {}
    sha = (meta.get("commit_sha") or "")[:7]
    lines = [
        "# Repository navigation (top level)\n",
        f"**{meta.get('owner', '')}/{meta.get('name', '')}** `{sha}`\n",
        "| Name | Type | Subfiles | Subfolders | Size (bytes) |",
        "| --- | --- | ---: | ---: | ---: |"
    ]
    for key, node in sorted((tree.get("children") or {}).items()):
        esc = key.replace("|", "\\|")
        if node.get("type") == "tree":
            lines.append(f"| `{esc}` | directory | {node.get('descendantFiles', 0)} | {node.get('descendantFolders', 0)} | — |")
        else:
            lines.append(f"| `{esc}` | file | — | — | {node.get('size', '')!s} |")
    lines.append("\n_Generated by Github-Discovery._")
    return "\n".join(lines)


def persist_discovery_map(tree: dict[str, Any], output_file: str) -> str:
    path = Path((output_file or "").strip()).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tree["saved_to"] = resolved = str(path.resolve())
    with path.open("w", encoding="utf-8") as f:
        json.dump(tree, f, indent=2, ensure_ascii=False)
    return resolved


def persist_navigation_markdown(tree: dict[str, Any], markdown_file: str) -> str:
    path = Path((markdown_file or "").strip()).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(render_navigation_markdown(tree))
    return str(path.resolve())


# --- 6. CORE ORCHESTRATION ---

async def discover_repository_async(
    url_or_repo: str, *, ref: Optional[str] = None, path_prefix: Optional[str] = None,
    max_tree_nodes: int = 50_000, token: Optional[str] = None,
    output_file: Optional[str] = None, markdown_file: Optional[str] = None
) -> dict[str, Any]:
    try:
        import httpx
    except ImportError as e:
        raise GitHubDiscoveryError("Requires httpx: pip install httpx") from e

    t0 = time.perf_counter()
    parsed = parse_github_url(url_or_repo)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "posthog-llma-discovery"}
    if tok := (token or os.environ.get("GITHUB_TOKEN")): headers["Authorization"] = f"Bearer {tok}"

    async with httpx.AsyncClient() as client:
        eff_ref = ref or parsed.ref
        if not eff_ref:
            repo_meta = await _github_request(client, "GET", f"/repos/{parsed.owner}/{parsed.repo}", headers=headers)
            eff_ref = repo_meta.get("default_branch", "main")

        commit_data = await _github_request(client, "GET", f"/repos/{parsed.owner}/{parsed.repo}/commits/{eff_ref}", headers=headers)
        commit_sha, tree_sha = commit_data["sha"], commit_data["commit"]["tree"]["sha"]

        tree_data = await _github_request(client, "GET", f"/repos/{parsed.owner}/{parsed.repo}/git/trees/{tree_sha}", headers=headers, params={"recursive": "1"})
        
        if tree_data.get("truncated"):
            api_strategy = "bfs_fallback"
            blobs = await _fetch_tree_bfs_flat(client, parsed.owner, parsed.repo, tree_sha, headers, max_tree_nodes)
        else:
            api_strategy = "recursive"
            blobs = [{"path": i["path"], "sha": i.get("sha", ""), "size": i.get("size")} for i in tree_data.get("tree", []) if i.get("type") == "blob"]

        blobs = filter_discovery_noise(_filter_by_prefix(blobs, path_prefix or parsed.path_prefix))
        summaries = await collect_smart_summaries(client, parsed.owner, parsed.repo, blobs, headers, limit=5)

    tree = build_tree_from_paths(blobs)
    tree["repo"] = {"owner": parsed.owner, "name": parsed.repo, "commit_sha": commit_sha, "ref": eff_ref}
    tree["integration_hints"] = suggest_integration_roots(tree)
    tree["discovery_stats"] = {
        "duration_seconds": time.perf_counter() - t0,
        "total_files": tree.get("descendantFiles", 0),
        "total_folders": tree.get("descendantFolders", 0),
        "total_size_bytes": sum(int(b["size"]) for b in blobs if b.get("size") is not None),
        "api_strategy": api_strategy,
        "tech_stack": detect_tech_stack(blobs),
        "smart_summaries": summaries if summaries else None,
    }

    out_f = str(output_file).strip() if output_file else ""
    md_target = (str(markdown_file).strip() if markdown_file is not None else (str(Path(out_f).with_name(f"{Path(out_f).stem}_nav.md")) if out_f else None))
    
    tree["markdown_saved_to"] = persist_navigation_markdown(tree, md_target) if md_target else None
    tree["saved_to"] = persist_discovery_map(tree, out_f) if out_f else None

    return tree


def discover_repository(url_or_repo: str, **kwargs) -> dict[str, Any]:
    return asyncio.run(discover_repository_async(url_or_repo, **kwargs))
