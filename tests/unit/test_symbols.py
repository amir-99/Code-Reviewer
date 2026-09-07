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
