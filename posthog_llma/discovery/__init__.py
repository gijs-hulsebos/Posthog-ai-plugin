"""
Local GitHub repository structure discovery.
Exposes the core orchestration engine and data types.
"""

from posthog_llma.discovery.engine import (
    GitHubDiscoveryError,
    ParsedRepoRef,
    discover_repository,
    discover_repository_async,
)

# We only export the entry points and the error class to keep the API surface lean.
# Internal logic (logic.py, heuristics.py) remains accessible via sub-modules 
# if a power user needs them, but they don't clutter the main namespace.
__all__ = [
    "GitHubDiscoveryError",
    "ParsedRepoRef",
    "discover_repository",
    "discover_repository_async",
]
