import os
import sys
import yaml
import argparse
from pathlib import Path

from kaos_ast.chunker import EXT_TO_LANG

def scan_codebase(target_dir: Path) -> set:
    """Scans the codebase and returns a set of detected file extensions."""
    detected_extensions = set()
    for root, dirs, files in os.walk(target_dir):
        # Ignore hidden directories like .git, .cocoindex_code
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for file in files:
            ext = Path(file).suffix.lower()
            if ext:
                detected_extensions.add(ext)
    return detected_extensions

def generate_settings(target_dir: Path, detected_extensions: set):
    """Generates the .cocoindex_code/settings.yml based on detected extensions."""
    settings_dir = target_dir / ".cocoindex_code"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_file = settings_dir / "settings.yml"
    
    include_patterns = []
    chunkers = []
    
    for ext in detected_extensions:
        if ext in EXT_TO_LANG:
            include_patterns.append(f"**/*{ext}")
            chunkers.append({
                "ext": ext.lstrip('.'),
                "module": "kaos_ast.chunker:custom_ast_chunker"
            })
            
    # Always include a default pattern if none are found to avoid empty config
    if not include_patterns:
        include_patterns.append("**/*.txt")
            
    config = {
        "include_patterns": include_patterns,
        "exclude_patterns": [
            "**/.*",
            "**/__pycache__",
            "**/node_modules",
            "**/dist",
            "**/build"
        ],
        "chunkers": chunkers
    }
    
    with open(settings_file, 'w') as f:
        yaml.dump(config, f, sort_keys=False)
        
    print(f"Generated cocoindex-code settings at {settings_file}", file=sys.stderr)
    print(f"Supported extensions detected: {[ext for ext in detected_extensions if ext in EXT_TO_LANG]}", file=sys.stderr)

def cli():
    parser = argparse.ArgumentParser(description="Kaos AST Context Provider using cocoindex-code and code_ast")
    parser.add_argument("target_dir", nargs="?", type=Path, default=Path("."),
                        help="Target codebase directory to scan and index (defaults to current directory)")
    parser.add_argument("--action", choices=["init", "index", "mcp"], default="index", help="Action to perform (init, index, mcp)")
    
    args = parser.parse_args()
    target_dir = args.target_dir.resolve()
    
    if not target_dir.exists():
        print(f"Error: Directory {target_dir} does not exist.")
        sys.exit(1)
        
    if args.action == "mcp":
        # MCP server handles all setup (settings, indexing) automatically
        import asyncio
        from kaos_ast.server import run_server
        asyncio.run(run_server())
    else:
        # For init/index: scan, generate settings, delegate to cocoindex CLI
        print(f"Scanning codebase at {target_dir}...")
        detected_extensions = scan_codebase(target_dir)
        generate_settings(target_dir, detected_extensions)

        original_cwd = os.getcwd()
        original_argv = sys.argv[:]
        try:
            os.chdir(target_dir)
            from cocoindex_code.cli import app as cocoindex_app
            sys.argv = ["ccc", args.action]
            cocoindex_app()
        except ImportError:
            print("Error: cocoindex-code is not installed.")
            print("Make sure you are running via 'uv run' or have installed the package.")
            sys.exit(1)
        finally:
            os.chdir(original_cwd)
            sys.argv = original_argv

if __name__ == "__main__":
    cli()
