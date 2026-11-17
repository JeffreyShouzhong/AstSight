"""C5a 代码枚举常量：扫描 Java/Python/TS 并折叠为取值语义。"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

from astsight.cli import main
from astsight.data import (
    build_catalog,
    parse_ddl,
    scan_code_enums,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


def test_scan_java_python_and_typescript_enums(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "OrderStatus.java",
        """
        // enum Fake { BROKEN }
        public enum OrderStatus {
            PENDING(0), // 待支付
            PAID(1),    // 已支付
            CANCELED(2);
            private final int code;
            OrderStatus(int code) { this.code = code; }
        }
        """,
    )
    _write(
        tmp_path / "src" / "user_status.py",
        """
        from enum import IntEnum

        class UserStatus(IntEnum):
            ACTIVE = 1  # 正常
            DISABLED = 0
        """,
    )
    _write(
        tmp_path / "src" / "level.ts",
        """
        export enum Level {
          LOW,
          HIGH,
        }
        """,
    )
    _write(
        tmp_path / "src" / "status.ts",
        """
        export enum OrderStatusType {
          PENDING = 'pending', // 待支付
          PAID = 'paid',
        }
        """,
    )
    _write(
        tmp_path / "node_modules" / "ignored.ts",
        "export enum Ignored { A = 'a' }",
    )

    scan = scan_code_enums(tmp_path, [tmp_path / "src"])
    by_name = {enum.name: enum for enum in scan.enums}
    assert set(by_name) == {"OrderStatus", "UserStatus", "Level", "OrderStatusType"}

    java = by_name["OrderStatus"]
    assert java.language == "java"
    assert java.path == "src/OrderStatus.java"
    assert [(item.name, item.value, item.meaning) for item in java.constants] == [
        ("PENDING", 0, "待支付"),
        ("PAID", 1, "已支付"),
        ("CANCELED", 2, ""),
    ]

    python = by_name["UserStatus"]
    assert python.language == "python"
    assert [(item.name, item.value) for item in python.constants] == [
        ("ACTIVE", 1),
        ("DISABLED", 0),
    ]
    assert python.constants[0].meaning == "正常"

    level = by_name["Level"]
    assert level.language == "typescript"
    assert [(item.name, item.value) for item in level.constants] == [
        ("LOW", 0),
        ("HIGH", 1),
    ]

    ts = by_name["OrderStatusType"]
    assert [(item.name, item.value, item.meaning) for item in ts.constants] == [
        ("PENDING", "pending", "待支付"),
        ("PAID", "paid", ""),
    ]
    assert "src/level.ts" in scan.files
    assert all("node_modules" not in path for path in scan.files)


def test_scan_non_enum_constant_groups(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "OrderStatus.java",
        """
        public final class OrderStatus {
            public static final String PENDING = "pending"; // 待支付
            public static final String PAID = "paid";
            private static final long serialVersionUID = 1L;
            public static final Logger LOG = LoggerFactory.getLogger(OrderStatus.class);
        }
        """,
    )
    _write(
        tmp_path / "src" / "TimeoutSettings.java",
        """
        public final class TimeoutSettings {
            public static final int READ = 10;
            public static final int WRITE = 20;
        }
        """,
    )
    _write(
        tmp_path / "src" / "order_status.go",
        """
        package order

        type Status int

        const (
            StatusPending Status = iota // 待支付
            StatusPaid
            StatusCanceled
        )
        """,
    )
    _write(
        tmp_path / "src" / "order_status.ts",
        """
        export const OrderStatus = {
          PENDING: 'pending', // 待支付
          PAID: 'paid',
        } as const;

        const Plain = { A: 'a', B: 'b' };
        """,
    )

    scan = scan_code_enums(tmp_path, [tmp_path / "src"])
    groups = {enum.path: enum for enum in scan.enums if enum.kind == "constants"}
    assert all(enum.name != "TimeoutSettings" for enum in scan.enums)

    java = groups["src/OrderStatus.java"]
    assert [(item.name, item.value, item.meaning) for item in java.constants] == [
        ("PENDING", "pending", "待支付"),
        ("PAID", "paid", ""),
    ]

    go = groups["src/order_status.go"]
    assert go.name == "OrderStatus"
    assert [(item.name, item.value) for item in go.constants] == [
        ("StatusPending", 0),
        ("StatusPaid", 1),
        ("StatusCanceled", 2),
    ]
    assert go.constants[0].meaning == "待支付"

    ts = groups["src/order_status.ts"]
    assert ts.name == "OrderStatus"
    assert [(item.name, item.value) for item in ts.constants] == [
        ("PENDING", "pending"),
        ("PAID", "paid"),
    ]
    # 没有 as const 的普通对象不当成常量组。
    assert all(enum.name != "Plain" for enum in scan.enums)


def test_constant_groups_fold_into_catalog(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "order_status.go",
        """
        package order

        const (
            StatusPending = "pending" // 待支付
            StatusPaid = "paid"
        )
        """,
    )
    scan = scan_code_enums(tmp_path, [tmp_path / "src"])
    catalog = build_catalog(
        project="demo",
        ddl=parse_ddl(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT);"
        ),
        code_enums=scan.enums,
    )
    status = catalog.table_index()["orders"].column_index()["status"]
    assert [(item.value, item.meaning, item.source) for item in status.value_semantics] == [
        ("pending", "待支付", "code:src/order_status.go"),
        ("paid", "StatusPaid", "code:src/order_status.go"),
    ]


def test_duplicate_identical_constant_groups_are_not_ambiguous(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "src" / "OrderStatus.java",
        """
        public final class OrderStatus {
            public static final String PENDING = "pending";
            public static final String PAID = "paid";
        }
        """,
    )
    _write(
        tmp_path / "src" / "order_status.ts",
        """
        export const OrderStatus = {
          PENDING: 'pending',
          PAID: 'paid',
        } as const;
        """,
    )
    scan = scan_code_enums(tmp_path, [tmp_path / "src"])
    catalog = build_catalog(
        project="demo",
        ddl=parse_ddl(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT);"
        ),
        code_enums=scan.enums,
    )
    status = catalog.table_index()["orders"].column_index()["status"]
    assert [item.value for item in status.value_semantics] == ["pending", "paid"]
    assert not any(
        warning.startswith("code_enum_ambiguous:")
        for warning in catalog.warnings
    )


def test_scan_missing_path_is_reported(tmp_path: Path) -> None:
    scan = scan_code_enums(tmp_path, [tmp_path / "missing"])
    assert scan.enums == []
    assert any(
        warning.startswith("code_path_missing:")
        for warning in scan.warnings
    )
    assert scan.files == []


def test_code_enum_is_folded_by_name_and_type(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "order_status.ts",
        """
        export enum OrderStatusType {
          PENDING = 'pending', // 待支付
          PAID = 'paid',
        }
        """,
    )
    scan = scan_code_enums(tmp_path, [tmp_path / "src"])
    catalog = build_catalog(
        project="demo",
        ddl=parse_ddl(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT);"
        ),
        code_enums=scan.enums,
    )
    status = catalog.table_index()["orders"].column_index()["status"]
    assert [(item.value, item.meaning, item.source) for item in status.value_semantics] == [
        ("pending", "待支付", "code:src/order_status.ts"),
        ("paid", "PAID", "code:src/order_status.ts"),
    ]
    assert catalog.sources["code_enums"] == {
        "files": ["src/order_status.ts"],
        "enums": ["OrderStatusType"],
        "used": ["code:src/order_status.ts"],
        "applied": 2,
    }


def test_code_enum_matches_database_values_even_with_unrelated_name(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "src" / "lifecycle.py",
        """
        from enum import StrEnum

        class Lifecycle(StrEnum):
            PENDING = 'pending'
            PAID = 'paid'
        """,
    )
    scan = scan_code_enums(tmp_path, [tmp_path / "src"])
    catalog = build_catalog(
        project="demo",
        ddl=parse_ddl(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, "
            "status TEXT CHECK (status IN ('pending', 'paid')));"
        ),
        code_enums=scan.enums,
    )
    status = catalog.table_index()["orders"].column_index()["status"]
    assert [(item.value, item.meaning) for item in status.value_semantics] == [
        ("pending", "PENDING"),
        ("paid", "PAID"),
    ]


def test_code_enum_value_mismatch_is_warned_and_skipped(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "status.py",
        """
        from enum import StrEnum

        class OrderStatus(StrEnum):
            PENDING = 'pending'
            PAID = 'paid'
        """,
    )
    scan = scan_code_enums(tmp_path, [tmp_path / "src"])
    catalog = build_catalog(
        project="demo",
        ddl=parse_ddl(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, "
            "status TEXT CHECK (status IN ('pending', 'lost')));"
        ),
        code_enums=scan.enums,
    )
    status = catalog.table_index()["orders"].column_index()["status"]
    assert [(item.value, item.meaning) for item in status.value_semantics] == [
        ("pending", "PENDING"),
        ("lost", "lost"),
    ]
    assert "code_enum_value_mismatch:orders.status:lost" in catalog.warnings


def test_ambiguous_code_enums_are_not_folded(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "a.ts",
        "export enum OrderStatusType { PENDING = 'pending' }",
    )
    _write(
        tmp_path / "src" / "b.ts",
        "export enum OrdersStatusType { PENDING = 'waiting' }",
    )
    scan = scan_code_enums(tmp_path, [tmp_path / "src"])
    catalog = build_catalog(
        project="demo",
        ddl=parse_ddl(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT);"
        ),
        code_enums=scan.enums,
    )
    status = catalog.table_index()["orders"].column_index()["status"]
    assert status.value_semantics == []
    assert any(
        warning.startswith("code_enum_ambiguous:orders.status:")
        for warning in catalog.warnings
    )


def test_data_catalog_cli_scans_code_enums(tmp_path: Path, capsys) -> None:
    _write(
        tmp_path / "schema.sql",
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT);",
    )
    _write(
        tmp_path / "src" / "order_status.py",
        """
        from enum import StrEnum

        class OrderStatus(StrEnum):
            PENDING = 'pending'  # 待支付
            PAID = 'paid'
        """,
    )
    code = main(
        [
            "data",
            "catalog",
            "--project",
            str(tmp_path),
            "--ddl",
            "schema.sql",
            "--code",
            "src",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["code_enums"] == 1
    assert payload["code_files"] == 1
    assert payload["semantic_values"] == 2
    catalog = json.loads(Path(payload["catalog"]).read_text(encoding="utf-8"))
    orders = next(table for table in catalog["tables"] if table["name"] == "orders")
    status = next(
        column for column in orders["columns"] if column["name"] == "status"
    )
    assert [(item["value"], item["meaning"], item["source"]) for item in status[
        "value_semantics"
    ]] == [
        ("pending", "待支付", "code:src/order_status.py"),
        ("paid", "PAID", "code:src/order_status.py"),
    ]
    assert catalog["sources"]["code_enums"]["applied"] == 2
