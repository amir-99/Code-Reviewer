from reviewer.services.symbols.index import SymbolIndex


def test_launch_languages():
    index = SymbolIndex()
    for path, text, name in [
        ("a.py", "def calculate():\n    return 1\n", "calculate"),
        ("a.go", "package a\nfunc Calculate() int { return 1 }", "Calculate"),
        ("a.ts", "function calculate(): number { return 1; }", "calculate"),
    ]:
        index.add(path, text)
        assert index.resolves(path, name)


def test_definition_forms_used_by_context_lookup():
    index = SymbolIndex()
    index.add(
        "a.ts",
        "interface Shape { area(): number }\n"
        "type Id = string\n"
        "const handler = (x: number) => {\n  return x\n}\n"
        "const limit = 3\n"
        "class Box {\n  size(): number {\n    return 1\n  }\n}\n",
    )
    for name in ("Shape", "Id", "handler", "Box", "size"):
        assert index.resolves("a.ts", name)
    assert not index.resolves("a.ts", "limit")
    assert index.enclosing("a.ts", 10)["name"] == "size"
    assert index.enclosing("a.ts", 7)["name"] == "Box"
    index.add("a.go", "package a\ntype Store struct{}\nfunc (s *Store) Get() {}\n")
    assert index.resolves("a.go", "Store") and index.resolves("a.go", "Get")
    assert index.enclosing("a.go", 1) is None
