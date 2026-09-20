"""RVK package with lazy compatibility exports for the historical core API.

Current ComfyUI nodes import their concrete modules directly. Keeping the old
root-level names lazy lets repository users retain the v0.1 API without making
the production plugin import the storage and recovery stack at startup.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORT_GROUPS = {
    ".contracts": (
        "AdapterContract",
        "AdapterIdentity",
        "AdapterRegistry",
        "AdapterState",
        "AssetRecord",
        "Commit",
        "ExportDescriptor",
        "Fingerprints",
        "FrameRange",
        "FramesPerSecond",
        "InputAsset",
        "JobRecord",
        "Plan",
        "Progress",
        "ResumeAsset",
        "SegmentIndexEntry",
        "SegmentRecord",
        "State",
    ),
    ".errors": (
        "AdapterUnsupported",
        "AssetInvalid",
        "CommitConflict",
        "CommitUncertain",
        "EncoderUnavailable",
        "JobConflict",
        "JobLocked",
        "NoProgress",
        "RVKError",
        "RecordInvalid",
        "StorageFull",
    ),
    ".locking": (
        "ACTIVE_RECORD_NAME",
        "CONTROLLER_LOCK_NAME",
        "SEGMENT_LOCK_NAME",
        "ActiveWriter",
        "SegmentLease",
        "SegmentPermit",
        "WindowsByteLock",
        "WriterSession",
        "admit_segment_worker",
        "decode_active_writer",
        "read_active_writer",
    ),
    ".records": (
        "CANONICALIZATION",
        "JOB_FORMAT",
        "SCHEMA_VERSION",
        "SEGMENT_FORMAT",
        "artifact_profile_fingerprint",
        "canonical_json",
        "decode_job",
        "decode_json",
        "decode_segment",
        "encode_job",
        "encode_segment",
        "generation_fingerprint",
        "hash_json",
        "job_record_hash",
        "project_identity_hash",
        "segment_commit_hash",
        "task_key",
    ),
    ".integrity": (
        "hash_input_file",
        "read_stable_bytes",
        "validate_relative_asset_path",
        "validate_storage_file",
        "validate_unique_asset_paths",
        "validate_windows_component",
        "verify_rebound_input",
    ),
    ".project": (
        "DirectoryProbe",
        "InitializedProject",
        "PreflightDisposition",
        "ProjectConfiguration",
        "ProjectLocation",
        "ProjectPreflight",
        "RootState",
        "initialize_project",
        "open_initialized_project",
        "prepare_project",
        "probe_project_root",
        "project_configuration",
        "select_project_location",
        "validate_project_root",
    ),
    ".storage": (
        "COMMIT_SYNC_POINTS",
        "PendingSegmentWriter",
        "commit_segment",
    ),
    ".recovery": (
        "RecordSource",
        "RecoveryStatus",
        "SegmentSettlement",
        "SettlementStatus",
        "VerifiedState",
        "revoke_permit",
        "settle_segment_attempt",
    ),
}

_EXPORTS = {
    name: module_name
    for module_name, names in _EXPORT_GROUPS.items()
    for name in names
}

__all__ = tuple(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
