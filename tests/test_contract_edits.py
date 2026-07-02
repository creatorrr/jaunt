from jaunt.contract.edits import add_contract_marker, remove_contract_marker


class TestAsyncAndClassMarkers:
    def test_add_marker_async_function(self) -> None:
        src = "async def f(x):\n    return x\n"
        out = add_contract_marker(src, "f")
        assert "@jaunt.contract\nasync def f(x):" in out
        assert out.startswith("import jaunt")

    def test_add_marker_class(self) -> None:
        src = "class C:\n    def m(self):\n        return 1\n"
        out = add_contract_marker(src, "C")
        assert "@jaunt.contract\nclass C:" in out

    def test_add_marker_class_above_existing_decorator(self) -> None:
        src = "import functools\n\n@functools.total_ordering\nclass C:\n    pass\n"
        out = add_contract_marker(src, "C")
        assert "@jaunt.contract\n@functools.total_ordering\nclass C:" in out

    def test_remove_marker_class_roundtrip(self) -> None:
        src = "class C:\n    def m(self):\n        return 1\n"
        marked = add_contract_marker(src, "C")
        assert remove_contract_marker(marked, "C").replace("import jaunt\n", "") == src

    def test_missing_name_error_mentions_class(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="function or class"):
            add_contract_marker("x = 1\n", "nope")
