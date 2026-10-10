from functools import cache
from pathlib import Path

import tree_sitter_go
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser

SUFFIXES = (".py", ".go", ".ts", ".tsx")

# Definitions named through their own `name` field.
NAMED = {
    "function_definition",
    "class_definition",
    "function_declaration",
    "method_declaration",
    "class_declaration",
    "abstract_class_declaration",
    "method_definition",
    "interface_declaration",
    "type_alias_declaration",
    "enum_declaration",
    "type_spec",
}


@cache
def parser(suffix):
    """One parser per language per process; construction is not free."""
    language = {
        ".py": lambda: tree_sitter_python.language(),
        ".go": lambda: tree_sitter_go.language(),
        ".ts": lambda: tree_sitter_typescript.language_typescript(),
        ".tsx": lambda: tree_sitter_typescript.language_tsx(),
    }.get(suffix)
    return Parser(Language(language())) if language else None


def definitions(path, text):
    """Named definitions in one source file, as {name, start, end} (1-based)."""
    active = parser(Path(path).suffix)
    if active is None:
        return []
    todo = [active.parse(text.encode()).root_node]
    found = []
    while todo:
        node = todo.pop()
        todo.extend(node.children)
        names = []
        if node.type in NAMED:
            names = [node.child_by_field_name("name")]
        elif node.type == "lexical_declaration":
            # `const handler = () => ...` names a definition through its declarator.
            # Plain values are not scopes, so only functions and classes count.
            names = [
                child.child_by_field_name("name")
                for child in node.children
                if child.type == "variable_declarator"
                and getattr(child.child_by_field_name("value"), "type", None)
                in {"arrow_function", "function_expression", "function", "class"}
            ]
        for name in names:
            if name is not None and name.type in {
                "identifier",
                "type_identifier",
                "field_identifier",
                "property_identifier",
            }:
                found.append(
                    {
                        "name": name.text.decode(),
                        "start": node.start_point.row + 1,
                        "end": node.end_point.row + 1,
                    }
                )
    return found


class SymbolIndex:
    def __init__(self):
        self.symbols = {}

    def add(self, path, text):
        if Path(path).suffix in SUFFIXES:
            self.symbols[path] = definitions(path, text)

    def resolves(self, path, name):
        return any(s["name"] == name for s in self.symbols.get(path, []))

    def enclosing(self, path, start, end=None):
        """The innermost definition containing the line range, or None."""
        end = end or start
        containing = [
            s
            for s in self.symbols.get(path, [])
            if s["start"] <= start and end <= s["end"]
        ]
        return min(containing, key=lambda s: s["end"] - s["start"], default=None)
