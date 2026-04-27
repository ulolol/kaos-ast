from __future__ import annotations

from pathlib import Path
import code_ast
from code_ast import ASTVisitor

import tree_sitter
import tree_sitter_typescript as _tst_pkg

# Mock or import Chunk from cocoindex_code.chunking
try:
    from cocoindex_code.chunking import Chunk, TextPosition
except ImportError:
    # Fallback definition for local testing without cocoindex
    from dataclasses import dataclass
    @dataclass
    class TextPosition:
        byte_offset: int
        char_offset: int
        line: int
        column: int

    @dataclass
    class Chunk:
        text: str
        start: TextPosition
        end: TextPosition

# Mapping of file extensions to code_ast/tree-sitter languages
EXT_TO_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php"
}

class KaosChunkVisitor(ASTVisitor):
    def __init__(self, content: str, path: Path):
        self.chunks: list[Chunk] = []
        self.content_lines = content.splitlines()
        self.path = path

    def _add_chunk(self, node, node_type: str):
        # Extract the source code for the node
        start_line = node.start_point[0]
        end_line = node.end_point[0]
        
        # Extract lines correctly
        if start_line < len(self.content_lines) and end_line < len(self.content_lines):
            # Includes the end line
            chunk_content = "\n".join(self.content_lines[start_line:end_line+1])
            
            # Create a Chunk with correct cocoindex-code API
            start_pos = TextPosition(
                byte_offset=node.start_byte,
                char_offset=node.start_byte, # Approximation
                line=node.start_point[0] + 1,
                column=node.start_point[1]
            )
            end_pos = TextPosition(
                byte_offset=node.end_byte,
                char_offset=node.end_byte, # Approximation
                line=node.end_point[0] + 1,
                column=node.end_point[1]
            )
            
            self.chunks.append(
                Chunk(
                    text=chunk_content,
                    start=start_pos,
                    end=end_pos
                )
            )

    # Python
    def visit_function_definition(self, node):
        self._add_chunk(node, "function")
        
    def visit_class_definition(self, node):
        self._add_chunk(node, "class")

    # JavaScript / TypeScript
    def visit_function_declaration(self, node):
        self._add_chunk(node, "function")

    def visit_class_declaration(self, node):
        self._add_chunk(node, "class")
        
    # Go
    def visit_method_declaration(self, node):
        self._add_chunk(node, "method")
        
    # Rust
    def visit_function_item(self, node):
        self._add_chunk(node, "function")

    # C/C++ — uses the same tree-sitter node type `function_definition` as Python,
    # so the visit_function_definition handler above covers both languages.


def _whole_file_chunk(content: str) -> Chunk:
    lines = content.splitlines()
    start_pos = TextPosition(byte_offset=0, char_offset=0, line=1, column=0)
    end_pos = TextPosition(
        byte_offset=len(content.encode('utf-8')),
        char_offset=len(content),
        line=len(lines) if lines else 1,
        column=len(lines[-1]) if lines else 0
    )
    return Chunk(text=content, start=start_pos, end=end_pos)


def _chunk_typescript(content: str, path: Path, lang: str) -> list[Chunk]:
    # tree_sitter_typescript ships TWO grammars (typescript + tsx) and exposes
    # them as language_typescript() / language_tsx() instead of the conventional
    # top-level .language() that code_ast._load_language expects. Bypass code_ast
    # for these two languages and drive tree_sitter directly.
    if lang == 'tsx':
        ts_lang = tree_sitter.Language(_tst_pkg.language_tsx())
    else:
        ts_lang = tree_sitter.Language(_tst_pkg.language_typescript())
    parser = tree_sitter.Parser(ts_lang)
    tree = parser.parse(content.encode('utf-8'))
    visitor = KaosChunkVisitor(content, path)
    visitor(tree.root_node)
    return visitor.chunks


def custom_ast_chunker(path: Path, content: str) -> tuple[str | None, list[Chunk]]:
    ext = path.suffix.lower()
    lang = EXT_TO_LANG.get(ext)

    if not lang:
        # Fallback to default if language is not supported
        return None, []

    try:
        if lang in ('typescript', 'tsx'):
            chunks = _chunk_typescript(content, path, lang)
        else:
            source_ast = code_ast.ast(content, lang=lang)
            visitor = KaosChunkVisitor(content, path)
            source_ast.visit(visitor)
            chunks = visitor.chunks

        # If no semantic chunks were found, fallback to returning the whole file as one chunk
        if not chunks:
            chunks = [_whole_file_chunk(content)]

        return lang, chunks
    except Exception as e:
        print(f"Error parsing {path} (lang={lang}): {e}", file=__import__('sys').stderr)
        return None, []

