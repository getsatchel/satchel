"""satchel-adapter-sdk: everything needed to write a satchel source adapter.

    from satchel_adapter_sdk import AdapterInfo, SourceAdapter, SourceFiles, SourceSnapshot
"""

from satchel_adapter_sdk.adapter import (
    KIND_PATTERN,
    AdapterInfo,
    Capability,
    FileInput,
    Maturity,
    SourceAdapter,
    SourceFiles,
)
from satchel_adapter_sdk.ir import (
    IR_VERSION,
    SourceAsset,
    SourceColumn,
    SourceQuery,
    SourceRelationship,
    SourceSemanticModel,
    SourceSnapshot,
    SourceTerm,
    SourceWarning,
    fingerprint_bytes,
    is_compatible,
    normalise_fqn,
)
from satchel_adapter_sdk.mdl import MdlBuilder, validate_mdl

__all__ = [
    "IR_VERSION",
    "KIND_PATTERN",
    "AdapterInfo",
    "Capability",
    "FileInput",
    "Maturity",
    "MdlBuilder",
    "SourceAdapter",
    "SourceAsset",
    "SourceColumn",
    "SourceFiles",
    "SourceQuery",
    "SourceRelationship",
    "SourceSemanticModel",
    "SourceSnapshot",
    "SourceTerm",
    "SourceWarning",
    "fingerprint_bytes",
    "is_compatible",
    "normalise_fqn",
    "validate_mdl",
]
