from pathlib import Path

import tree_sitter_go
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser


class SymbolIndex:
    def __init__(self):
        self.parsers = {
            ".py": Parser(Language(tree_sitter_python.language())),
            ".go": Parser(Language(tree_sitter_go.language())),
            ".ts": Parser(Language(tree_sitter_typescript.language_typescript())),
            ".tsx": Parser(Language(tree_sitter_typescript.language_tsx())),
        }
        self.symbols = {}

    def add(self, path, text):
        parser = self.parsers.get(Path(path).suffix)
        if parser is None:
            return
        root = parser.parse(text.encode()).root_node
        todo = [root]
        found = []
        while todo:
            node = todo.pop()
            todo.extend(node.children)
            if node.type in {
                "function_definition",
                "class_definition",
                "function_declaration",
                "method_declaration",
                "type_declaration",
                "class_declaration",
                "method_definition",
                "lexical_declaration",
            }:
                name = node.child_by_field_name("name")
                if name:
                    found.append(
                        {
                            "name": name.text.decode(),
                            "start": node.start_point.row + 1,
                            "end": node.end_point.row + 1,
                        }
                    )
        self.symbols[path] = found

    def resolves(self, path, name):
        return any(s["name"] == name for s in self.symbols.get(path, []))
