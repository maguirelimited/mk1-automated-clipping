"""Funnel Management MK1 — canonical funnel models and schema validation."""

from .funnel_templates import (
    FunnelTemplate,
    FunnelTemplateError,
    build_funnel_from_template,
    get_funnel_template,
    list_funnel_templates,
)
from .importer import ExistingFunnelImporter, FunnelImportError, FunnelImportReport
from .sync import (
    FunnelSyncError,
    FunnelSyncFileChange,
    FunnelSyncReport,
    FunnelSyncTargetPaths,
    FunnelSynchronizer,
)
from .validation import (
    FunnelValidationIssue,
    FunnelValidationReport,
    FunnelValidationSeverity,
    FunnelValidator,
)
from .registry import (
    DuplicateFunnelError,
    FunnelNotFoundError,
    FunnelRegistry,
    FunnelRegistryError,
    FunnelRegistryPathError,
    default_registry_dir,
)
from .schema import (
    CanonicalFunnel,
    CanonicalFunnelSchemaError,
    dump_canonical_funnel,
    load_canonical_funnel,
)

__all__ = [
    "CanonicalFunnel",
    "CanonicalFunnelSchemaError",
    "DuplicateFunnelError",
    "ExistingFunnelImporter",
    "FunnelImportError",
    "FunnelImportReport",
    "FunnelTemplate",
    "FunnelTemplateError",
    "FunnelSyncError",
    "FunnelSyncFileChange",
    "FunnelSyncReport",
    "FunnelSyncTargetPaths",
    "FunnelSynchronizer",
    "FunnelValidationIssue",
    "FunnelValidationReport",
    "FunnelValidationSeverity",
    "FunnelValidator",
    "build_funnel_from_template",
    "get_funnel_template",
    "list_funnel_templates",
    "FunnelNotFoundError",
    "FunnelRegistry",
    "FunnelRegistryError",
    "FunnelRegistryPathError",
    "default_registry_dir",
    "dump_canonical_funnel",
    "load_canonical_funnel",
]
