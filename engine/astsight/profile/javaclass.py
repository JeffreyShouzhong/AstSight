"""最小 Java class 文件解析（只读，不执行）。

用途：在拿不到 B 的源码时，从 A 依赖的 api/client jar 里读出 DTO 字段和
Feign 接口签名，作为下游 Mock 的契约依据。

只解析本模块需要的结构：常量池、字段、方法、类/方法上的
RuntimeVisibleAnnotations（Feign 注解靠它）。其余属性按长度跳过。

不做的事：泛型擦除后的类型推断、Lombok 生成逻辑、反射语义。
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import Any

_MAGIC = 0xCAFEBABE
_MAX_CLASS_BYTES = 8 * 1024 * 1024

_PRIMITIVES = {
    "B": "byte",
    "C": "char",
    "D": "double",
    "F": "float",
    "I": "int",
    "J": "long",
    "S": "short",
    "Z": "boolean",
    "V": "void",
}


class ClassFileError(ValueError):
    """class 文件不合法或超出解析边界。"""


@dataclass(frozen=True)
class ClassField:
    name: str
    descriptor: str
    signature: str = ""
    access: int = 0

    @property
    def is_static(self) -> bool:
        return bool(self.access & 0x0008)

    @property
    def is_enum_constant(self) -> bool:
        return bool(self.access & 0x4000)

    def type_name(self) -> str:
        return descriptor_to_type(self.descriptor, self.signature)


@dataclass(frozen=True)
class ClassMethod:
    name: str
    descriptor: str
    access: int = 0
    annotations: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class ClassInfo:
    name: str
    access: int = 0
    super_name: str | None = None
    interfaces: list[str] = field(default_factory=list)
    fields: list[ClassField] = field(default_factory=list)
    methods: list[ClassMethod] = field(default_factory=list)
    annotations: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def simple_name(self) -> str:
        return self.name.rsplit(".", 1)[-1]


class _Reader:
    __slots__ = ("data", "pos", "size")

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.size = len(data)

    def take(self, count: int) -> bytes:
        end = self.pos + count
        if end > self.size:
            raise ClassFileError("class 文件截断")
        chunk = self.data[self.pos : end]
        self.pos = end
        return chunk

    def u1(self) -> int:
        return self.take(1)[0]

    def u2(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def u4(self) -> int:
        return struct.unpack(">I", self.take(4))[0]


# 常量池 tag → 额外需要读取的字节数；None 表示 8 字节且占两个槽位。
def _read_constant_pool(reader: _Reader) -> list[Any]:
    count = reader.u2()
    if count <= 1 or count > 65_535:
        raise ClassFileError(f"常量池大小异常: {count}")
    pool: list[Any] = [None] * count
    index = 1
    while index < count:
        tag = reader.u1()
        if tag == 1:  # Utf8
            length = reader.u2()
            raw = reader.take(length)
            pool[index] = ("utf8", raw.decode("utf-8", errors="replace"))
        elif tag in (3, 4):  # Integer / Float
            pool[index] = ("num", reader.take(4))
        elif tag in (5, 6):  # Long / Double，占两个槽位
            pool[index] = ("num", reader.take(8))
            index += 2
            continue
        elif tag in (7, 8, 16, 19, 20):  # Class / String / MethodType / Module / Package
            pool[index] = (tag, reader.u2())
        elif tag in (9, 10, 11, 12, 17, 18):  # ref / NameAndType / Dynamic / InvokeDynamic
            pool[index] = (tag, reader.u4())
        elif tag == 15:  # MethodHandle
            pool[index] = (tag, reader.u1(), reader.u2())
        else:
            raise ClassFileError(f"未知常量池 tag: {tag}")
        index += 1
    return pool


def _utf8(pool: list[Any], index: int) -> str:
    if not 0 < index < len(pool):
        return ""
    entry = pool[index]
    if isinstance(entry, tuple) and entry[0] == "utf8":
        return str(entry[1])
    return ""


def _class_name(pool: list[Any], index: int) -> str:
    if not 0 < index < len(pool):
        return ""
    entry = pool[index]
    if isinstance(entry, tuple) and entry[0] == 7:
        return _utf8(pool, int(entry[1])).replace("/", ".")
    return ""


def _parse_element_value(reader: _Reader, pool: list[Any]) -> Any:
    tag = chr(reader.u1())
    if tag in "BCDFIJSZs":
        index = reader.u2()
        if tag == "s":
            return _utf8(pool, index)
        entry = pool[index] if 0 < index < len(pool) else None
        raw = entry[1] if isinstance(entry, tuple) and entry[0] == "num" else b""
        if tag in "IJ":
            return int.from_bytes(raw.ljust(8, b"\x00"), "big", signed=tag == "J")
        if tag in "BCS":
            return int.from_bytes(raw.ljust(4, b"\x00"), "big", signed=tag == "S")
        if tag == "Z":
            return bool(int.from_bytes(raw.ljust(4, b"\x00"), "big"))
        if tag == "F":
            return struct.unpack(">f", raw.ljust(4, b"\x00"))[0]
        if tag == "D":
            return struct.unpack(">d", raw.ljust(8, b"\x00"))[0]
        return None
    if tag == "e":  # enum
        type_index = reader.u2()
        const_index = reader.u2()
        return {
            "enum": _utf8(pool, type_index),
            "value": _utf8(pool, const_index),
        }
    if tag == "c":  # class
        return _utf8(pool, reader.u2())
    if tag == "@":  # nested annotation
        name, values = _parse_annotation(reader, pool)
        return {"annotation": name, "values": values}
    if tag == "[":  # array
        count = reader.u2()
        return [_parse_element_value(reader, pool) for _ in range(count)]
    raise ClassFileError(f"未知注解元素 tag: {tag}")


def _parse_annotation(reader: _Reader, pool: list[Any]) -> tuple[str, dict[str, Any]]:
    type_index = reader.u2()
    raw_type = _utf8(pool, type_index)
    name = raw_type
    if name.startswith("L") and name.endswith(";"):
        name = name[1:-1].replace("/", ".")
    name = name.rsplit(".", 1)[-1]
    pairs = reader.u2()
    values: dict[str, Any] = {}
    for _ in range(pairs):
        key = _utf8(pool, reader.u2())
        values[key] = _parse_element_value(reader, pool)
    return name, values


def _parse_annotations(reader: _Reader, pool: list[Any]) -> dict[str, dict[str, Any]]:
    count = reader.u2()
    result: dict[str, dict[str, Any]] = {}
    for _ in range(count):
        name, values = _parse_annotation(reader, pool)
        result[name] = values
    return result


def _skip_attributes(reader: _Reader, pool: list[Any]) -> None:
    count = reader.u2()
    for _ in range(count):
        reader.u2()
        length = reader.u4()
        reader.take(length)


def _read_attributes(
    reader: _Reader, pool: list[Any], *, want: set[str]
) -> dict[str, bytes]:
    """返回关注的原始属性内容，其余按长度跳过。"""
    count = reader.u2()
    found: dict[str, bytes] = {}
    for _ in range(count):
        name = _utf8(pool, reader.u2())
        length = reader.u4()
        payload = reader.take(length)
        if name in want and name not in found:
            found[name] = payload
    return found


def _parse_annotations_payload(payload: bytes, pool: list[Any]) -> dict[str, dict[str, Any]]:
    reader = _Reader(payload)
    return _parse_annotations(reader, pool)


def parse_class(data: bytes) -> ClassInfo:
    if len(data) > _MAX_CLASS_BYTES:
        raise ClassFileError("class 文件超过解析上限")
    reader = _Reader(data)
    if reader.u4() != _MAGIC:
        raise ClassFileError("不是 class 文件")
    reader.u2()  # minor
    major = reader.u2()
    if major < 45 or major > 80:
        raise ClassFileError(f"class 版本不在支持范围: {major}")
    pool = _read_constant_pool(reader)
    access = reader.u2()
    this_class = reader.u2()
    super_class = reader.u2()
    name = _class_name(pool, this_class)
    if not name:
        raise ClassFileError("class 缺少 this_class")
    info = ClassInfo(
        name=name,
        access=access,
        super_name=_class_name(pool, super_class) or None,
    )
    for _ in range(reader.u2()):
        info.interfaces.append(_class_name(pool, reader.u2()))

    for _ in range(reader.u2()):
        field_access = reader.u2()
        field_name = _utf8(pool, reader.u2())
        descriptor = _utf8(pool, reader.u2())
        attrs = _read_attributes(reader, pool, want={"Signature"})
        signature = ""
        if "Signature" in attrs and len(attrs["Signature"]) >= 2:
            signature = _utf8(pool, struct.unpack(">H", attrs["Signature"][:2])[0])
        info.fields.append(
            ClassField(
                name=field_name,
                descriptor=descriptor,
                signature=signature,
                access=field_access,
            )
        )

    for _ in range(reader.u2()):
        method_access = reader.u2()
        method_name = _utf8(pool, reader.u2())
        descriptor = _utf8(pool, reader.u2())
        attrs = _read_attributes(
            reader, pool, want={"RuntimeVisibleAnnotations"}
        )
        annotations: dict[str, dict[str, Any]] = {}
        if "RuntimeVisibleAnnotations" in attrs:
            try:
                annotations = _parse_annotations_payload(
                    attrs["RuntimeVisibleAnnotations"], pool
                )
            except ClassFileError:
                annotations = {}
        info.methods.append(
            ClassMethod(
                name=method_name,
                descriptor=descriptor,
                access=method_access,
                annotations=annotations,
            )
        )

    attrs = _read_attributes(reader, pool, want={"RuntimeVisibleAnnotations"})
    if "RuntimeVisibleAnnotations" in attrs:
        try:
            info.annotations = _parse_annotations_payload(
                attrs["RuntimeVisibleAnnotations"], pool
            )
        except ClassFileError:
            info.annotations = {}
    return info


# ───────────────────────── 类型描述符 ─────────────────────────

class _SignatureParser:
    """JVM 泛型签名（JVMS 4.7.9.1）的最小解析器。"""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def parse(self) -> str:
        value = self._type()
        if self.pos != len(self.text):
            raise ClassFileError("签名未完全解析")
        return value

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def _type(self) -> str:
        char = self._peek()
        if not char:
            raise ClassFileError("签名截断")
        if char == "[":
            self.pos += 1
            return self._type() + "[]"
        if char == "L":
            return self._class_type()
        if char == "T":
            self.pos += 1
            start = self.pos
            while self.pos < len(self.text) and self.text[self.pos] != ";":
                self.pos += 1
            name = self.text[start : self.pos]
            self.pos += 1
            return name
        self.pos += 1
        return _PRIMITIVES.get(char, char)

    def _class_type(self) -> str:
        self.pos += 1  # L
        name: list[str] = []
        args: list[str] = []
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "<":
                self.pos += 1
                args = self._type_args()
                continue
            if char == ";":
                self.pos += 1
                break
            if char == ".":
                name.append("$")
                self.pos += 1
                continue
            name.append(char)
            self.pos += 1
        base = "".join(name).replace("/", ".")
        if args:
            base += "<" + ", ".join(args) + ">"
        return base

    def _type_args(self) -> list[str]:
        result: list[str] = []
        while self.pos < len(self.text) and self.text[self.pos] != ">":
            char = self.text[self.pos]
            if char == "*":
                result.append("?")
                self.pos += 1
            elif char in "+-":
                self.pos += 1
                value = self._type()
                result.append(("? extends " if char == "+" else "? super ") + value)
            else:
                result.append(self._type())
        if self.pos < len(self.text) and self.text[self.pos] == ">":
            self.pos += 1
        return result


def descriptor_to_type(descriptor: str, signature: str = "") -> str:
    """把字段描述符/泛型签名转成可读类型；签名优先。"""
    if signature:
        try:
            return _SignatureParser(signature).parse()
        except (ClassFileError, IndexError):
            pass
    return _descriptor_to_text(descriptor)


def _descriptor_to_text(descriptor: str) -> str:
    text = str(descriptor or "")
    dims = 0
    while text.startswith("["):
        dims += 1
        text = text[1:]
    if text.startswith("L") and text.endswith(";"):
        base = text[1:-1].replace("/", ".").replace("$", ".")
    else:
        base = _PRIMITIVES.get(text, text)
    return base + "[]" * dims


__all__ = [
    "ClassField",
    "ClassFileError",
    "ClassInfo",
    "ClassMethod",
    "descriptor_to_type",
    "parse_class",
]
