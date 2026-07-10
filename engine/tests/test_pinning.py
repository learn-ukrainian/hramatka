"""PIN tests: only pinned inputs affect engine identity, and tampering is refused.

Two guarantees the whole boundary rests on:
  1. an UNPINNED file (outside the vendor dir / not in the data manifest) cannot
     change the bake fingerprint or the resolved identity — the engine reads
     digests from manifests, never a directory scan;
  2. a vendor-digest mismatch, and a data-bundle digest mismatch, are REFUSED.
"""

from __future__ import annotations

import shutil

import pytest

from hramatka.engine import data, pipeline, vendoring

_FP_KW = dict(
    anchor_hash="fixed-anchor-hash",
    level="B1",
    pedagogy="ttt",
    phase="1",
    types=["true-false", "cloze", "match-up"],
    grounding_text="fixed grounding",
    prompt_template="fixed prompt",
)


# ---------------------------------------------------------------------------
# 1) Unpinned files do not change identity
# ---------------------------------------------------------------------------
def test_unpinned_file_in_data_dir_does_not_change_fingerprint():
    fp_before = pipeline.make_fingerprint(**_FP_KW)
    digests_before = data.active_bundle().digests()

    # Drop an unpinned file into the active bundle's release dir (a tmp dir).
    stray = data.active_bundle().root / "STRAY-unpinned.bin"
    stray.write_bytes(b"noise that is not in the data manifest")

    # digests come from the manifest, not a dir scan -> unchanged; still verifies.
    assert data.active_bundle().digests() == digests_before
    data.active_bundle().verify()
    assert pipeline.make_fingerprint(**_FP_KW) == fp_before


def test_unpinned_file_in_vendor_dir_does_not_change_identity(tmp_path, monkeypatch):
    versions_before = vendoring.artifact_versions()

    vendor_copy = tmp_path / "vendor"
    shutil.copytree(vendoring.VENDOR_ROOT, vendor_copy)
    (vendor_copy / vendoring.LU_ACTIVITY / "STRAY-unpinned.txt").write_text("noise")
    monkeypatch.setattr(vendoring, "VENDOR_ROOT", vendor_copy)

    # The loader only hashes manifest-listed files -> identity unchanged.
    assert vendoring.artifact_versions() == versions_before
    vendoring.verify_all()


# ---------------------------------------------------------------------------
# 2) Digest mismatches are refused
# ---------------------------------------------------------------------------
def test_vendor_digest_mismatch_is_refused(tmp_path, monkeypatch):
    vendor_copy = tmp_path / "vendor"
    shutil.copytree(vendoring.VENDOR_ROOT, vendor_copy)
    # Tamper a PINNED file without updating the manifest.
    target = vendor_copy / vendoring.LU_ACTIVITY / "activities-b1.schema.json"
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    monkeypatch.setattr(vendoring, "VENDOR_ROOT", vendor_copy)
    vendoring._module_cache.clear()

    with pytest.raises(vendoring.VendorIntegrityError):
        vendoring.verify_artifact(vendoring.LU_ACTIVITY)
    with pytest.raises(vendoring.VendorIntegrityError):
        vendoring.read_json(vendoring.LU_ACTIVITY, "activities-b1.schema.json")


def test_data_digest_mismatch_is_refused(tmp_path):
    (tmp_path / "vesum.db").write_bytes(b"pretend corpus bytes")
    drifted = {
        "bundle": "test",
        "version": "test",
        "inputs": {
            "vesum.db": {"path": "vesum.db", "sha256": "0" * 64, "size": 1, "required": True}
        },
    }
    with pytest.raises(data.DataDriftError):
        data.resolve_bundle(data_dir=tmp_path, manifest=drifted, verify=True)

    # The explicit escape hatch bypasses refusal (deliberate, logged drift).
    bundle = data.resolve_bundle(
        data_dir=tmp_path, manifest=drifted, verify=True, allow_drift=True
    )
    assert bundle.digests() == {"vesum.db": "0" * 64}
