from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import capabilityhub.release_certification as release_certification
from capabilityhub.metering import canonical_json
from capabilityhub.release_certification import (
    REQUIRED_EVIDENCE,
    ReleaseCertificationError,
    build_release_subject,
    certify_release,
    evidence_document,
    load_release_subject,
    main,
    verify_certification,
    verify_release_subject_artifacts,
)

NOW = datetime(2026, 8, 12, 6, tzinfo=UTC)
REVISION = "b" * 40
DIGEST = "a" * 64
SIGNING_KEY = b"release-certification-test-signing-key-material"


def _metrics(name: str) -> dict[str, object]:
    return {
        "adversarial": {"passed": True, "external_provider_cases_min": 5},
        "browser": {"passed": True, "assertions_min": 12},
        "docs_traceability": {"passed": True, "claims_min": 36},
        "full_pytest": {"failures": 0, "passed_min": 760, "unexpected_skips": 0},
        "matrix_36": {"implemented": 36, "partial": 0, "total": 36},
        "model_live": {
            "live": True,
            "provider_reported_tokens": True,
            "trials_min": 30,
        },
        "mypy": {"errors": 0},
        "rag_1m": {"chunks_min": 1_000_000, "production_index": True},
        "ruff": {"violations": 0},
        "sandbox_linux": {"enforced": True, "platform": "linux"},
        "sandbox_windows": {"enforced": True, "platform": "windows"},
        "search_10k": {"catalog_min": 10_000, "top3_correct": True},
        "supply_bundle": {"verified": True, "online_freshness": True},
        "wheel": {"smoke_passed": True, "wheels_min": 1},
    }[name]


def _evidence(
    tmp_path,
    *,
    changes=None,
    created_at=NOW - timedelta(minutes=5),
    subject_digest=DIGEST,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    paths = []
    changes = changes or {}
    for name in REQUIRED_EVIDENCE:
        overrides = changes.get(name, {})
        document = evidence_document(
            name,
            source_revision=overrides.get("source_revision", REVISION),
            subject_digest=overrides.get("subject_digest", subject_digest),
            metrics=overrides.get("metrics", _metrics(name)),
            created_at=overrides.get("created_at", created_at),
            status=overrides.get("status", "passed"),
            skipped=overrides.get("skipped", 0),
        )
        path = tmp_path / f"evidence-{name}.json"
        path.write_text(canonical_json(document), encoding="utf-8")
        paths.append(path)
    return paths


def _subject(tmp_path):
    source = tmp_path / "source.tar"
    wheel = tmp_path / "capabilityhub.whl"
    source.write_bytes(b"source-archive")
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("capabilityhub/__init__.py", b'__version__ = "test"\n')
    document = build_release_subject(
        source,
        wheel,
        source_revision=REVISION,
        sbom_path=tmp_path / "sbom.json",
    )
    path = tmp_path / "subject.json"
    path.write_text(canonical_json(document), encoding="utf-8")
    return path, document


def test_complete_fresh_evidence_generates_deterministic_signed_manifest(tmp_path) -> None:
    paths = _evidence(tmp_path)

    first = certify_release(
        paths,
        source_revision=REVISION,
        subject_digest=DIGEST,
        signing_key=SIGNING_KEY,
        key_id="release-key",
        now=NOW,
    )
    second = certify_release(
        reversed(paths),
        source_revision=REVISION,
        subject_digest=DIGEST,
        signing_key=SIGNING_KEY,
        key_id="release-key",
        now=NOW,
    )

    assert first == second
    assert first.manifest["evidence_count"] == len(REQUIRED_EVIDENCE)
    verify_certification(first.manifest, first.signature, signing_key=SIGNING_KEY)
    tampered = dict(first.manifest)
    tampered["source_revision"] = "tampered"
    with pytest.raises(ReleaseCertificationError) as invalid:
        verify_certification(tampered, first.signature, signing_key=SIGNING_KEY)
    assert invalid.value.code == "release_signature_invalid"


def test_build_once_subject_binds_source_wheel_and_generated_sbom(tmp_path) -> None:
    path, document = _subject(tmp_path)
    loaded = load_release_subject(path, source_revision=REVISION)

    assert loaded == document
    assert set(loaded["artifacts"]) == {"source", "wheel", "sbom"}
    sbom = json.loads((tmp_path / "sbom.json").read_text(encoding="utf-8"))
    assert sbom["schema"] == "capabilityhub.release-sbom.v1"
    assert sbom["members"][0]["path"] == "capabilityhub/__init__.py"
    verify_release_subject_artifacts(loaded, tmp_path)

    (tmp_path / "capabilityhub.whl").write_bytes(b"replaced")
    with pytest.raises(ReleaseCertificationError) as replaced:
        verify_release_subject_artifacts(loaded, tmp_path)
    assert replaced.value.code == "release_subject_artifact_mismatch"

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["artifacts"]["wheel"]["size"] += 1
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ReleaseCertificationError) as failed:
        load_release_subject(path, source_revision=REVISION)
    assert failed.value.code == "release_subject_digest_mismatch"


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        ({"sandbox_linux": {"skipped": 1}}, "release_evidence_skipped"),
        ({"model_live": {"status": "skipped"}}, "release_evidence_not_passed"),
        (
            {"model_live": {"metrics": {"live": False, "trials_min": 30}}},
            "release_evidence_metric_failed",
        ),
        (
            {"sandbox_windows": {"source_revision": "old"}},
            "release_evidence_source_mismatch",
        ),
        (
            {"matrix_36": {"metrics": {"implemented": 35, "partial": 1, "total": 36}}},
            "release_evidence_metric_failed",
        ),
    ),
)
def test_skipped_partial_fake_live_and_mixed_revision_fail_closed(
    tmp_path, changes, code
) -> None:
    paths = _evidence(tmp_path, changes=changes)

    with pytest.raises(ReleaseCertificationError) as failed:
        certify_release(
            paths,
            source_revision=REVISION,
            subject_digest=DIGEST,
            signing_key=SIGNING_KEY,
            key_id="release-key",
            now=NOW,
        )
    assert failed.value.code == code


def test_stale_duplicate_missing_and_unexpected_evidence_fail_closed(tmp_path) -> None:
    stale_paths = _evidence(tmp_path / "stale", created_at=NOW - timedelta(hours=25))
    with pytest.raises(ReleaseCertificationError) as stale:
        certify_release(
            stale_paths,
            source_revision=REVISION,
            subject_digest=DIGEST,
            signing_key=SIGNING_KEY,
            key_id="key",
            now=NOW,
        )
    assert stale.value.code == "release_evidence_stale"

    paths = _evidence(tmp_path / "complete")
    with pytest.raises(ReleaseCertificationError) as duplicate:
        certify_release(
            [*paths, paths[0]],
            source_revision=REVISION,
            subject_digest=DIGEST,
            signing_key=SIGNING_KEY,
            key_id="key",
            now=NOW,
        )
    assert duplicate.value.code == "release_evidence_duplicate"
    with pytest.raises(ReleaseCertificationError) as missing:
        certify_release(
            paths[:-1],
            source_revision=REVISION,
            subject_digest=DIGEST,
            signing_key=SIGNING_KEY,
            key_id="key",
            now=NOW,
        )
    assert missing.value.code == "release_evidence_missing"


def test_cli_refuses_to_certify_without_signing_key_or_live_evidence(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    subject_path, subject = _subject(tmp_path)
    _evidence(
        root,
        changes={"model_live": {"status": "skipped", "skipped": 30}},
        subject_digest=subject["subject_digest"],
    )
    arguments = [
        "certify",
        "--evidence-root",
        str(root),
        "--source-revision",
        REVISION,
        "--subject",
        str(subject_path),
        "--subject-artifact-root",
        str(tmp_path),
        "--key-id",
        "release-key",
        "--manifest",
        str(tmp_path / "manifest.json"),
        "--signature",
        str(tmp_path / "signature.json"),
    ]
    monkeypatch.delenv("CAPABILITYHUB_RELEASE_SIGNING_KEY", raising=False)
    with pytest.raises(ReleaseCertificationError) as missing_key:
        main(arguments)
    assert missing_key.value.code == "release_signing_key_missing"

    monkeypatch.setenv("CAPABILITYHUB_RELEASE_SIGNING_KEY", SIGNING_KEY.decode())
    with pytest.raises(ReleaseCertificationError) as skipped_live:
        main(arguments)
    assert skipped_live.value.code == "release_evidence_not_passed"
    assert not (tmp_path / "manifest.json").exists()
    assert not (tmp_path / "signature.json").exists()


def test_release_workflow_uses_build_once_subject_and_program_measured_gates() -> None:
    workflow = Path(".github/workflows/release-certification.yml").read_text(
        encoding="utf-8"
    )

    assert "build-subject:" in workflow
    assert "git archive --format=tar" in workflow
    assert "release_certification subject" in workflow
    assert "release_certification gate --type" in workflow
    assert " release_certification record " not in workflow
    assert "--metric" not in workflow
    assert "tests/test_linux_sandbox.py" in workflow
    assert "gate --type sandbox_linux" in workflow
    assert "CAPABILITYHUB_REQUIRE_LINUX_SANDBOX" in workflow
    assert "model_eval --live --trials 30 --source-revision" in workflow
    assert "--subject release-build/release-subject.json" in workflow


def test_full_suite_accepts_only_the_separately_certified_browser_skip(tmp_path) -> None:
    report = tmp_path / "report.xml"
    report.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite tests="2" skipped="2">
    <testcase classname="tests.browser.test_dashboard_browser" name="browser">
      <skipped message="collection skipped">
        tests/browser/test_dashboard_browser.py:10:
        could not import 'playwright.sync_api': No module named playwright
      </skipped>
    </testcase>
    <testcase classname="tests.test_runtime" name="unexpected">
      <skipped message="unrelated dependency unavailable" />
    </testcase>
  </testsuite>
</testsuites>
""",
        encoding="utf-8",
    )

    assert release_certification._unexpected_platform_skips(report) == 1
