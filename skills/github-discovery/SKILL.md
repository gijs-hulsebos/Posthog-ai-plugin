---
name: github-discovery
description: >
  Github-Discovery Skill. Use this skill to rapidly map the internal structure of any GitHub repository.
  This is your primary tool for gaining codebase context, verifying ground-truth file paths, and fetching
  smart summaries of entry points before reasoning about code.
---

# Github-Discovery Skill

## Instruction

Use this skill to discover and map the internal structure of any GitHub repository. This is your primary tool for gaining general codebase context and locating specific application logic.

Prefer **ground-truth paths** from the discovery map over guessing directory names. 

Github-Discovery is a **high-performance navigation suite**: it returns a filtered tree, **tech stack hints**, concurrent **short previews** of the most important files, optional **JSON + Markdown** artifacts, and **operational stats** so you can plan your investigation depth before burning context tokens.

## Intelligent ignore (noise reduction)

To save your context window, the engine automatically **drops** paths that are rarely useful for code reasoning:

- **Directory segments** (entire subtree skipped): `node_modules`, `.git`, `__pycache__`, `dist`, `build`, `.next`, `.nuxt`, `coverage`, `.venv`, `venv`, `target`.
- **Files (basename)**: `.DS_Store`, common **lockfiles** (`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `poetry.lock`, `Pipfile.lock`, `composer.lock`, `Gemfile.lock`, `go.sum`, `Cargo.lock`, `bun.lockb`, `npm-shrinkwrap.json`).

Counts in `discovery_stats` and the returned tree reflect this cleanly filtered view, not the raw GitHub tree.

## Using `tech_stack` to adjust analysis

`discovery_stats.tech_stack` lists inferred tags from **root-level manifest files** (e.g., `nodejs`, `python`, `rust`, `go`, `docker`, `compose`, `typescript`, `nextjs`, `vite`, `nuxt`, `jvm-maven`, `jvm-gradle`, `ruby`).

**How to use it:**

- Treat `tech_stack` as a **routing signal**. Combine it with `smart_summaries` and the tree before assuming versions or frameworks.
- **Order your investigation** by stack: e.g., if `python` appears, prioritize `pyproject.toml` / `requirements.txt` branches and Python entrypoints; if `nodejs` + `typescript`, prioritize `package.json`, `tsconfig.json`, and `src/` TypeScript trees.
- If `tech_stack` is **empty**, fall back to the README from `smart_summaries`, then widen with a scoped `path_prefix` discovery.
- For **polyglot** repos (many tags), split work by directory (`path_prefix`) instead of one flat analysis.

## Smart summaries (`content_preview`)

`discovery_stats.smart_summaries` (when present) lists up to **five** high-value files (e.g., READMEs, root manifests, common entrypoints). Each item includes:

| Field | Meaning |
| ----- | ------- |
| `path` | Repository path |
| `sha` | Git blob SHA |
| `content_preview` | First **500** UTF-8 characters (decoded concurrently from the Git blob API) |
| `importance_score` | Internal ranking (higher = more central) |

Use previews to **bootstrap context** before opening full files. Respect repository scale rules and do not rely on previews alone for complex logic or security-sensitive conclusions.

## Tool input schema (Github-Discovery)

```json
{
  "url": "https://github.com/org/repo",
  "ref": "main",
  "path_prefix": "src",
  "max_tree_nodes": 50000,
  "token": null,
  "output_file": "docs/maps/repo-map.json",
  "markdown_file": null
}
```

| Field | Required | Type | Description |
| ----- | -------- | ---- | ----------- |
| `url` or `repo` | yes | string | HTTPS GitHub URL or `owner/repo` |
| `ref` | no | string | Branch, tag, or commit (overrides ref parsed from a `/tree/...` URL when provided) |
| `path_prefix` | no | string | Only map paths under this directory (output paths are relative to that prefix) |
| `max_tree_nodes` | no | integer | Safety cap when GitHub truncates very large recursive tree responses (default 50000) |
| `token` | no | string | Per-call token; otherwise `GITHUB_TOKEN` is used |
| `output_file` | no | string | If set, writes the full discovery `map` as UTF-8 JSON with `indent=2` to this path; parent directories are created. Relative paths resolve from the process working directory. |
| `markdown_file` | no | string | If a non-empty string, writes the **navigation Markdown table** there. If omitted and `output_file` ends with `.json`, a sibling `*_nav.md` is written automatically. Pass `""` to **disable** Markdown when saving JSON. |

## Best practice — persistence

For large repositories, **always provide an `output_file`** (e.g., `docs/maps/repo_name.json`). This allows the user to inspect the tree on disk and lets you reload that JSON in a future session instead of re-scanning GitHub.

On success, `map.saved_to` is the absolute path where the JSON was written (or `null` if omitted). 

When Markdown is emitted, `map.markdown_saved_to` holds the absolute path to the **top-level navigation table** (or `null` if skipped). The Markdown file is optimized for quick scanning: one row per immediate child of the discovery root.

## Handling repository scale

Before analyzing code, always check `discovery_stats` on the returned `map`.

- If `total_files` > 500 **or** `total_size_bytes` > 5242880 (5 MiB), **do not** attempt to read or ingest every file from the repository in one pass.
- Instead, issue a **follow-up** `Github-Discovery` call with `path_prefix` set to a meaningful subdirectory (e.g., `src/`, `lib/`, `backend/`) so the tree and downstream reads stay within a safe token budget.
- Prefer multiple scoped discoveries over one monolithic full-repo analysis when stats indicate a large footprint.

This prevents context-window overflow and keeps token usage highly predictable.

## Technical specs — JSON output

The tool returns `{"ok": true, "map": <tree>}` on success. The `map` matches a nested directory tree:

### Node types
- **`tree`**: A directory. Has `children` (object map keyed by **segment name**), plus iterative rollup counts.
- **`blob`**: A file. `children` is always `{}`. Includes `sha` and `size` when available from the Git API.

### Fields (every node)
| Field | Meaning |
| ----- | ------- |
| `name` | Final path segment (`root` for the synthetic root) |
| `path` | POSIX path from repo root (or relative to `path_prefix` when scoped) |
| `type` | `"tree"` or `"blob"` |
| `children` | Map of child name → child node (empty for blobs) |
| `descendantFiles` | Total **file** count in the subtree (excluding the node itself) |
| `descendantFolders` | Total **folder** count in the subtree (excluding the node itself) |

### Extra top-level metadata on `map`
- **`repo`**: `{ owner, name, commit_sha, ref }` — exact commit resolved for the request.
- **`discovery_stats`**: Operational intelligence for scale and latency decisions.
- **`saved_to`**: Absolute path where the map was written when `output_file` was provided.
- **`markdown_saved_to`**: Absolute path to the navigation Markdown file.

On failure the handler returns `{"ok": false, "error": "<message>"}` (e.g. missing `httpx`, rate limit, bad URL) — read `error` and adjust `ref`, token, or prefix before retrying.
