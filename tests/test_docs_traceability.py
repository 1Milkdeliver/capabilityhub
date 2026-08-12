from pathlib import Path

from scripts.docs_traceability import STALE_PHRASES, traceability_errors


def test_release_claims_have_runtime_and_test_evidence() -> None:
    assert traceability_errors() == ()


def test_stale_release_wording_fails_closed(tmp_path: Path) -> None:
    root = tmp_path
    for source in (
        "README.md",
        "docs/completion-matrix.md",
        "docs/release-readiness.md",
    ):
        target = root / source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("release facts\n", encoding="utf-8")
    (root / "README.md").write_text(next(iter(STALE_PHRASES)), encoding="utf-8")

    errors = traceability_errors(root)

    assert any("stale release wording remains" in error for error in errors)


def test_readme_links_complete_english_and_chinese_user_guides() -> None:
    root = Path(__file__).parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    english = (root / "docs" / "user-guide-en.md").read_text(encoding="utf-8")
    chinese = (root / "docs" / "user-guide-zh-CN.md").read_text(encoding="utf-8")

    assert "[English user guide](docs/user-guide-en.md)" in readme
    assert "[中文使用说明书](docs/user-guide-zh-CN.md)" in readme
    assert english.count("\n## ") == chinese.count("\n## ") == 13
    for command in ("/helpme", "/myskills", "capsift dashboard --project-root ."):
        assert command in english
        assert command in chinese
