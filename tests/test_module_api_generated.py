from __future__ import annotations

from jaunt.module_api import build_generated_class_api_summary, generated_public_api_digest

GEN = (
    "class Inv:\n"
    '    """generated docs"""\n'
    "    def add(self, item, qty): return None\n"
    "    def total(self): return 0\n"
    "    def _bump(self): return 1\n"
)


def test_hybrid_summary_reads_generated_members_uses_spec_doc() -> None:
    s = build_generated_class_api_summary(GEN, "Inv", spec_docstring="SPEC DOC")
    names = {m.name for m in s.members}
    assert names == {"add", "total"}  # private _bump excluded
    assert s.doc == "SPEC DOC"


def test_hybrid_summary_white_box_includes_private() -> None:
    s = build_generated_class_api_summary(GEN, "Inv", spec_docstring="x", public_api_only=False)
    assert "_bump" in {m.name for m in s.members}


def test_generated_public_api_digest_ignores_private_changes() -> None:
    other = GEN.replace("_bump", "_bump2").replace("return 1", "return 2")
    assert generated_public_api_digest(GEN, "Inv") == generated_public_api_digest(other, "Inv")


def test_generated_public_api_digest_changes_on_public_change() -> None:
    changed = GEN.replace("def total(self)", "def total(self, scope)")
    assert generated_public_api_digest(GEN, "Inv") != generated_public_api_digest(changed, "Inv")
