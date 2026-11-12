"""C5 数据层：DDL/seed → catalog；approved cases → data plan；多方言执行。"""
from .apply import (
    ApplyError,
    ApplyResult,
    apply_bundle,
    apply_plan,
    parse_sqlite_url,
    teardown_plan,
)
from .catalog import CATALOG_SCHEMA_VERSION, build_catalog
from .ddl import (
    DDLError,
    ParsedColumn,
    ParsedDDL,
    ParsedForeignKey,
    ParsedTable,
    parse_ddl,
    split_statements,
)
from .dialects import (
    DbTarget,
    DialectError,
    SUPPORTED_DIALECTS,
    dialect_from_url,
    normalize_dialect,
    parse_db_url,
)
from .plan import PlanError, build_data_plan, build_data_plan_bundle
from .render import RenderError, quote_identifier, render_inserts, render_plan, render_teardown
from .seed import SeedData, SeedError, load_seed
from .semantics import SemanticApplyResult, SemanticDraft, apply_semantics
from .validate import (
    check_type,
    validate_catalog,
    validate_fixtures,
    validate_fk_closure,
    validate_plan_fixtures,
)

__all__ = [
    "ApplyError",
    "ApplyResult",
    "CATALOG_SCHEMA_VERSION",
    "DDLError",
    "DbTarget",
    "DialectError",
    "ParsedColumn",
    "ParsedDDL",
    "ParsedForeignKey",
    "ParsedTable",
    "PlanError",
    "RenderError",
    "SUPPORTED_DIALECTS",
    "SeedData",
    "SeedError",
    "SemanticApplyResult",
    "SemanticDraft",
    "apply_bundle",
    "apply_plan",
    "apply_semantics",
    "build_catalog",
    "build_data_plan",
    "build_data_plan_bundle",
    "check_type",
    "dialect_from_url",
    "load_seed",
    "normalize_dialect",
    "parse_db_url",
    "parse_ddl",
    "parse_sqlite_url",
    "quote_identifier",
    "render_inserts",
    "render_plan",
    "render_teardown",
    "split_statements",
    "teardown_plan",
    "validate_catalog",
    "validate_fixtures",
    "validate_fk_closure",
    "validate_plan_fixtures",
]
