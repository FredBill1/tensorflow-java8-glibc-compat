"""Inspect TensorFlow ELF libraries and enforce the portable Linux ABI contract.

The parser uses only Python's standard library and does not execute native code.
"""
import argparse
import json
import mmap
from pathlib import Path
import re
import struct
import sys


LIMITS = {"GLIBC": "2.27", "GLIBCXX": "3.4.22", "CXXABI": "1.3.11"}
MACHINES = {"linux-x86_64": 62, "linux-arm64": 183}
VERSION = re.compile(r"^(GLIBC|GLIBCXX|CXXABI)_(\d+(?:\.\d+)*)$")


def _version(value):
    return tuple(int(part) for part in value.split("."))


def inspect_library(path):
    """Return JSON-serializable ELF imports, loader paths, and JNI exports."""
    path = Path(path)
    with path.open("rb") as stream:
        if path.stat().st_size < 64:
            raise ValueError("Not an ELF64 library: " + str(path))
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            if data[:5] != b"\x7fELF\x02" or data[5] not in (1, 2):
                raise ValueError("Not an ELF64 library: " + str(path))
            order = "<" if data[5] == 1 else ">"

            def unpack(fmt, offset):
                size = struct.calcsize(order + fmt)
                if offset < 0 or offset + size > len(data):
                    raise ValueError("Truncated ELF structure: " + str(path))
                return struct.unpack_from(order + fmt, data, offset)

            def string(offset, end):
                if offset < 0 or offset >= end or end > len(data):
                    raise ValueError("Invalid ELF string offset: " + str(path))
                terminator = data.find(b"\0", offset, end)
                if terminator < 0:
                    raise ValueError("Unterminated ELF string: " + str(path))
                return data[offset:terminator].decode("utf-8")

            header = unpack("16sHHIQQQIHHHHHH", 0)
            section_offset, section_size, count, names_index = header[6], header[11], header[12], header[13]
            if section_size < 64 or not count or names_index >= count:
                raise ValueError("Unsupported or absent ELF section table: " + str(path))
            sections = [unpack("IIQQQQIIQQ", section_offset + i * section_size) for i in range(count)]
            names = sections[names_index]
            named = {string(names[4] + section[0], names[4] + names[5]): section for section in sections}
            requirements = {}
            requirement_section = named.get(".gnu.version_r")
            if requirement_section:
                strings = sections[requirement_section[6]]
                position, end = requirement_section[4], requirement_section[4] + requirement_section[5]
                while position < end:
                    _, item_count, filename, auxiliary, next_item = unpack("HHIII", position)
                    library = string(strings[4] + filename, strings[4] + strings[5])
                    versions = requirements.setdefault(library, [])
                    auxiliary_position = position + auxiliary
                    for _ in range(item_count):
                        _, _, _, version_name, next_auxiliary = unpack("IHHII", auxiliary_position)
                        versions.append(string(strings[4] + version_name, strings[4] + strings[5]))
                        auxiliary_position += next_auxiliary
                    if not next_item:
                        break
                    position += next_item

            maxima = {name: None for name in LIMITS}
            private_versions = []
            for versions in requirements.values():
                for name in versions:
                    match = VERSION.fullmatch(name)
                    if match:
                        family, value = match.groups()
                        if maxima[family] is None or _version(value) > _version(maxima[family]):
                            maxima[family] = value
                    elif name == "GLIBC_PRIVATE":
                        private_versions.append(name)

            needed, rpaths, path_tags = [], [], []
            dynamic = named.get(".dynamic")
            if dynamic:
                strings = sections[dynamic[6]]
                for position in range(dynamic[4], dynamic[4] + dynamic[5], 16):
                    tag, value = unpack("QQ", position)
                    if tag == 0:
                        break
                    if tag in (1, 15, 29):
                        value = string(strings[4] + value, strings[4] + strings[5])
                        if tag == 1:
                            needed.append(value)
                        else:
                            path_tags.append({"tag": "RPATH" if tag == 15 else "RUNPATH", "value": value})
                            rpaths.extend(value.split(":"))

            exports = set()
            symbols = named.get(".dynsym")
            if symbols:
                strings = sections[symbols[6]]
                if symbols[9] < 24:
                    raise ValueError("Invalid ELF dynamic symbol size: " + str(path))
                for position in range(symbols[4], symbols[4] + symbols[5], symbols[9]):
                    name, info, other, section_index, _, _ = unpack("IBBHQQ", position)
                    if section_index and info >> 4 in (1, 2) and other & 3 in (0, 3):
                        name = string(strings[4] + name, strings[4] + strings[5])
                        if name.startswith(("Java_", "JNI_OnLoad", "JNI_OnUnload")):
                            exports.add(name)

            return {"file": str(path.resolve()), "elf_class": 64,
                    "endianness": "little" if order == "<" else "big",
                    "elf_type": header[1], "machine": header[2], "max_versions": maxima,
                    "needed": needed, "rpaths": rpaths, "path_tags": path_tags,
                    "version_requirements": requirements, "private_versions": sorted(set(private_versions)),
                    "jni_exports": sorted(exports)}


def validate_library(path, platform):
    """Inspect a shared library, raising ValueError on architecture/ABI/path failure."""
    if platform not in MACHINES:
        raise ValueError("Unsupported native platform: " + platform)
    result = inspect_library(path)
    errors = []
    if result["machine"] != MACHINES[platform] or result["endianness"] != "little" or result["elf_type"] != 3:
        errors.append("expected a little-endian {} shared library (machine {})".format(platform, MACHINES[platform]))
    for family, limit in LIMITS.items():
        actual = result["max_versions"][family]
        if actual is not None and _version(actual) > _version(limit):
            errors.append("{} {} exceeds {}".format(family, actual, limit))
    if result["private_versions"]:
        errors.append("depends on private glibc symbols")
    for entry in result["rpaths"]:
        if entry.startswith("/"):
            errors.append("absolute loader path is not portable: " + entry)
    if errors:
        raise ValueError(str(path) + ": " + "; ".join(errors))
    result["platform"] = platform
    result["limits"] = dict(LIMITS)
    result["validated"] = True
    return result


def compare_jni_exports(rebuilt, official):
    """Require exactly the official Java_/JNI lifecycle export-name set."""
    rebuilt_names = set(inspect_library(rebuilt)["jni_exports"])
    official_names = set(inspect_library(official)["jni_exports"])
    missing, unexpected = sorted(official_names - rebuilt_names), sorted(rebuilt_names - official_names)
    if not official_names or missing or unexpected:
        raise ValueError("JNI export mismatch: official_count={} missing={} unexpected={}".format(
            len(official_names), missing, unexpected))
    return {"matching": True, "count": len(official_names), "missing": [], "unexpected": []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("path")
    validate = subparsers.add_parser("validate")
    validate.add_argument("platform", choices=sorted(MACHINES))
    validate.add_argument("path")
    compare = subparsers.add_parser("compare-jni")
    compare.add_argument("rebuilt")
    compare.add_argument("official")
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = inspect_library(args.path)
        elif args.command == "validate":
            result = validate_library(args.path, args.platform)
        else:
            result = compare_jni_exports(args.rebuilt, args.official)
    except (OSError, ValueError, struct.error) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
