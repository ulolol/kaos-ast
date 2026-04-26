"""Kaos AST MCP Server.

Extends cocoindex-code's MCP server with additional tools:
- `status`: Reports indexing status, chunk/file counts, and language breakdown.

The server auto-provisions all configuration on startup:
1. Scans the target directory for code files
2. Generates cocoindex project settings with our custom AST chunker
3. Creates global user settings (sentence-transformers default) if missing
4. Kicks off background indexing
5. Serves MCP tools (search + status) immediately

No manual `init` or `index` step required.
"""

from __future__ import annotations

import asyncio
import sys
import os
from pathlib import Path
from urllib.parse import urlparse, unquote

from mcp.server.fastmcp import FastMCP, Context
from pydantic import BaseModel, Field

# -----------------------------------------------------------------------------
# MCP Server Definition
# -----------------------------------------------------------------------------

# We use instructions adapted from cocoindex-code
_MCP_INSTRUCTIONS = (
    "Provide semantic code search across the entire codebase."
    " Use `search` to find code by meaning instead of just text matching."
    " Always check `status` before searching if you haven't recently,"
    " to ensure the index is ready."
)

mcp = FastMCP("kaos-ast", instructions=_MCP_INSTRUCTIONS)

# Global cache for the resolved roots
_resolved_roots: list[str] | None = None

class StatusResult(BaseModel):
    """Result from the status tool."""
    project_root: list[str] = Field(description="Absolute paths to the indexed project roots")
    index_exists: bool = Field(description="Whether an index has been built")
    indexing_in_progress: bool = Field(description="Whether indexing is currently running")
    total_chunks: int = Field(default=0, description="Total number of indexed code chunks")
    total_files: int = Field(default=0, description="Total number of indexed files")
    languages: dict[str, int] = Field(
        default_factory=dict,
        description="Map of language name to chunk count",
    )
    message: str | None = Field(
        default=None,
        description="Human-readable summary or error message",
    )

class CodeChunkResult(BaseModel):
    """A single code chunk result from a semantic search."""
    file_path: str = Field(description="Path to the file relative to the project root")
    language: str | None = Field(description="Programming language of the chunk (if detected)")
    content: str = Field(description="The actual code content")
    start_line: int = Field(description="1-indexed starting line number")
    end_line: int = Field(description="1-indexed ending line number")
    score: float = Field(description="Relevance score (higher is better)")

class SearchResultModel(BaseModel):
    """Result from the search tool."""
    success: bool = Field(description="Whether the search succeeded")
    results: list[CodeChunkResult] = Field(default_factory=list, description="List of matching code chunks")
    total_returned: int = Field(default=0, description="Number of chunks returned in this payload")
    offset: int = Field(default=0, description="The offset applied to this query (for pagination)")
    message: str | None = Field(default=None, description="Human-readable summary or error message")

# -----------------------------------------------------------------------------
# Configuration and Initialization
# -----------------------------------------------------------------------------

def _ensure_user_settings() -> None:
    """Create global user settings with sentence-transformers default if missing."""
    from cocoindex_code.settings import (
        default_user_settings,
        save_user_settings,
        user_settings_path,
    )

    if not user_settings_path().is_file():
        settings = default_user_settings()
        from cocoindex_code.embedder_defaults import lookup_defaults
        _, query_defaults = lookup_defaults(
            settings.embedding.provider, settings.embedding.model
        )
        settings.embedding.indexing_params = {}
        settings.embedding.query_params = query_defaults or {}
        save_user_settings(settings)
        print(
            f"Auto-created user settings at {user_settings_path()} "
            f"(model: {settings.embedding.model})",
            file=sys.stderr,
        )

def _ensure_project_settings(project_root: str) -> None:
    """Generate cocoindex project settings for the given root."""
    from kaos_ast.main import generate_settings, scan_codebase
    root = Path(project_root)
    detected_extensions = scan_codebase(root)
    generate_settings(root, detected_extensions)

def _trigger_bg_index(project_roots: list[str]) -> None:
    """Trigger background indexing for all specified roots."""
    from cocoindex_code.cli import _bg_index
    for root in project_roots:
        _ensure_project_settings(root)
        asyncio.create_task(_bg_index(root))

async def _resolve_roots(ctx: Context) -> list[str]:
    """
    Lazily resolves the workspace roots using the MCP roots capability.
    Caches the results so we don't query the client on every tool call.
    If roots cannot be retrieved, falls back to the current working directory.
    Triggers indexing on first resolution.
    """
    global _resolved_roots
    if _resolved_roots is not None:
        return _resolved_roots

    roots = []
    try:
        if hasattr(ctx, 'session') and ctx.session:
            # Query the client for roots
            roots_result = await ctx.session.list_roots()
            if roots_result and hasattr(roots_result, 'roots'):
                for root in roots_result.roots:
                    uri = root.uri
                    # Parse file:// URIs to local paths
                    parsed = urlparse(uri)
                    if parsed.scheme == 'file':
                        # url2pathname equivalents
                        path = unquote(parsed.path)
                        roots.append(path)
    except Exception as e:
        print(f"Error querying roots capability: {e}", file=sys.stderr)

    if not roots:
        # Fallback to current working directory
        roots = [os.getcwd()]
        print(f"Roots capability not available or empty, falling back to CWD: {roots[0]}", file=sys.stderr)
    else:
        print(f"Resolved roots via MCP protocol: {roots}", file=sys.stderr)

    _resolved_roots = roots
    
    # Auto-provision settings (non-interactive) globally
    _ensure_user_settings()
    
    # Auto-provision project settings and kick off indexing for each root
    _trigger_bg_index(roots)
    
    return _resolved_roots

# -----------------------------------------------------------------------------
# MCP Tools
# -----------------------------------------------------------------------------

@mcp.tool()
async def set_roots(paths: list[str]) -> str:
    """
    Override the currently indexed workspace directories.
    
    Use this if the auto-detected roots are incorrect, or if you want to extend
    semantic search to cover additional external libraries or directories.
    
    Args:
        paths: List of absolute directory paths to index and search.
    """
    global _resolved_roots
    
    # Verify paths exist
    valid_paths = []
    for path in paths:
        p = Path(path).resolve()
        if p.exists() and p.is_dir():
            valid_paths.append(str(p))
        else:
            return f"Error: Path '{path}' is not a valid existing directory."
            
    if not valid_paths:
        return "Error: No valid paths provided."
        
    _resolved_roots = valid_paths
    
    # Re-provision and trigger indexing for new roots
    _ensure_user_settings()
    _trigger_bg_index(valid_paths)
    
    return f"Roots successfully set to {valid_paths}. Background indexing has started."

@mcp.tool(
    description=(
        "Check the current indexing status of the codebase."
        " Returns whether an index exists, how many files and code chunks"
        " are indexed, which languages were detected, and whether"
        " indexing is currently in progress."
        " Use this to verify the index is ready before searching,"
        " or to report index health to the user."
    )
)
async def status(ctx: Context) -> StatusResult:
    """Query the project status from the cocoindex daemon."""
    from cocoindex_code import client as _client
    
    roots = await _resolve_roots(ctx)
    loop = asyncio.get_event_loop()
    
    # Aggregate status across all roots
    all_index_exists = True
    any_indexing = False
    total_chunks = 0
    total_files = 0
    all_languages = {}
    messages = []
    
    for root in roots:
        try:
            resp = await loop.run_in_executor(
                None, lambda r=root: _client.project_status(r)
            )
            
            all_index_exists = all_index_exists and resp.index_exists
            any_indexing = any_indexing or resp.indexing
            total_chunks += resp.total_chunks
            total_files += resp.total_files
            
            for lang, count in resp.languages.items():
                all_languages[lang] = all_languages.get(lang, 0) + count
                
            if not resp.index_exists:
                messages.append(f"[{root}] Index not created yet.")
            elif resp.progress is not None:
                messages.append(f"[{root}] Indexing: {resp.progress.num_adds} added, {resp.progress.num_errors} errors.")
            else:
                messages.append(f"[{root}] Ready.")
                
        except Exception as e:
            all_index_exists = False
            messages.append(f"[{root}] Error getting status: {e!s}")

    lang_summary = ", ".join(f"{lang} ({count})" for lang, count in sorted(all_languages.items(), key=lambda x: -x[1]))
    
    summary = "\n".join(messages)
    if all_index_exists and not any_indexing:
        summary = f"Ready. {total_files} files, {total_chunks} chunks across: {lang_summary}.\n" + summary

    return StatusResult(
        project_root=roots,
        index_exists=all_index_exists,
        indexing_in_progress=any_indexing,
        total_chunks=total_chunks,
        total_files=total_files,
        languages=all_languages,
        message=summary,
    )

@mcp.tool(
    description=(
        "Semantic code search across the entire codebase"
        " -- finds code by meaning, not just text matching."
        " Use this instead of grep/glob when you need to find implementations,"
        " understand how features work,"
        " or locate related code without knowing exact names or keywords."
        " Accepts natural language queries"
        " (e.g., 'authentication logic', 'database connection handling')"
        " or code snippets."
        " Returns matching code chunks with file paths,"
        " line numbers, and relevance scores."
        " Start with a small limit (e.g., 5);"
        " if most results look relevant, use offset to paginate for more."
    )
)
async def search(
    ctx: Context,
    query: str = Field(
        description=(
            "Natural language query or code snippet to search for."
            " Examples: 'error handling middleware',"
            " 'how are users authenticated',"
            " 'database connection pool',"
            " or paste a code snippet to find similar code."
        )
    ),
    limit: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Maximum number of results to return (1-100)",
    ),
    offset: int = Field(
        default=0,
        ge=0,
        description="Number of results to skip for pagination",
    ),
    refresh_index: bool = Field(
        default=True,
        description=(
            "Whether to incrementally update the index before searching."
            " Set to False for faster consecutive queries"
            " when the codebase hasn't changed."
        ),
    ),
    languages: list[str] | None = Field(
        default=None,
        description="Filter by programming language(s). Example: ['python', 'typescript']",
    ),
    paths: list[str] | None = Field(
        default=None,
        description=(
            "Filter by file path pattern(s) using GLOB wildcards (* and ?)."
            " Example: ['src/utils/*', '*.py']"
        ),
    ),
) -> SearchResultModel:
    """Query the codebase index via the daemon."""
    from cocoindex_code import client as _client
    
    roots = await _resolve_roots(ctx)
    loop = asyncio.get_event_loop()
    
    all_results = []
    
    # Note: If there are multiple roots, we search them all and merge results.
    # We apply the limit/offset to the merged results.
    for root in roots:
        try:
            if refresh_index:
                await loop.run_in_executor(None, lambda r=root: _client.index(r))
                
            resp = await loop.run_in_executor(
                None,
                lambda r=root: _client.search(
                    project_root=r,
                    query=query,
                    languages=languages,
                    paths=paths,
                    # Request more so we can merge and sort properly
                    limit=limit + offset, 
                    offset=0,
                ),
            )
            
            if resp.success:
                # Add root hint to file_path if multiple roots
                prefix = f"[{Path(root).name}] " if len(roots) > 1 else ""
                
                for r in resp.results:
                    all_results.append(
                        CodeChunkResult(
                            file_path=prefix + r.file_path,
                            language=r.language,
                            content=r.content,
                            start_line=r.start_line,
                            end_line=r.end_line,
                            score=r.score,
                        )
                    )
        except Exception as e:
            print(f"Query failed for root {root}: {e}", file=sys.stderr)

    # Sort merged results by score
    all_results.sort(key=lambda x: x.score, reverse=True)
    
    # Apply offset and limit
    paged_results = all_results[offset:offset+limit]
    
    success = len(all_results) > 0 or len(roots) > 0
    message = f"Found {len(all_results)} total results" if success else "No results found or all queries failed."
    
    return SearchResultModel(
        success=success,
        results=paged_results,
        total_returned=len(paged_results),
        offset=offset,
        message=message,
    )

# -----------------------------------------------------------------------------
# Entry Point
# -----------------------------------------------------------------------------

async def run_server() -> None:
    """Start the Kaos AST MCP server."""
    # Settings will be provisioned on first tool call
    await mcp.run_stdio_async()

