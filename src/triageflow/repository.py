"""Bounded, read-only tools for untrusted repository contents.

These tools inspect UTF-8 source and documentation; they never import or execute it.
The configured root is an application-owned trust boundary, not an OS sandbox.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path, PureWindowsPath
from typing import Any


class RepositoryError(ValueError):
    """A repository request is invalid, inaccessible, or outside the read policy."""


class Repository:
    """Expose a small source-only view of one configured repository directory."""

    MAX_FILE_BYTES = 256 * 1024
    MAX_READ_BYTES = 32 * 1024
    MAX_SCAN_BYTES = 8 * 1024 * 1024
    MAX_FILES = 400
    MAX_ENTRIES = 2_000
    MAX_DEPTH = 12
    MAX_LINES = 120
    MAX_QUERY_LENGTH = 200
    MAX_MATCH_TEXT = 500

    SOURCE_EXTENSIONS = frozenset(
        ".py .pyi .js .jsx .ts .tsx .mjs .cjs .java .go .rs .c .h .cpp .cc .hpp "
        ".cs .rb .php .swift .kt .kts .scala .sh .bash .zsh .ps1 .psm1 .sql "
        ".html .htm .css .scss .sass .less .json .toml .yaml .yml .xml .md .rst "
        ".txt .ini .cfg .vue .svelte .graphql .gql .proto .dart .ex .exs .erl "
        ".hrl .clj .cljs .cljc .edn .fs .fsx .vb .r .lua .pl .cmake".split()
    )
    SOURCE_NAMES = frozenset({"dockerfile", "makefile", "license", "notice"})
    EXCLUDED_DIRECTORIES = frozenset(
        {
            "node_modules",
            "vendor",
            "dist",
            "build",
            "coverage",
            "__pycache__",
            "venv",
            "env",
            "target",
            "site-packages",
            "secrets",
            "credentials",
        }
    )
    _SENSITIVE_NAME = re.compile(
        r"(?:^|[._-])(?:env|secrets?|credentials?|tokens?|passwords?|"
        r"private[._-]?keys?|id_rsa|id_ed25519)(?:$|[._-])",
        re.IGNORECASE,
    )
    _WINDOWS_DEVICE = re.compile(r"^(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)", re.I)

    def __init__(self, root: str | Path):
        try:
            supplied = Path(root).absolute()
            if self._is_link(supplied.lstat()):
                raise RepositoryError("Repository root must not be a link or reparse point.")
            resolved = supplied.resolve(strict=True)
            if not resolved.is_dir():
                raise RepositoryError("Repository root must be an existing directory.")
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            if isinstance(exc, RepositoryError):
                raise
            raise RepositoryError("Repository root must be an existing directory.") from None
        self.root = resolved

    @staticmethod
    def _is_link(info: os.stat_result) -> bool:
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )

    @classmethod
    def _allowed_component(cls, name: str) -> bool:
        return bool(name) and not (
            name.startswith(".")
            or name.endswith((".", " "))
            or ":" in name
            or any(ord(char) < 32 for char in name)
            or cls._SENSITIVE_NAME.search(name)
            or cls._WINDOWS_DEVICE.match(name)
        )

    @classmethod
    def _source_name(cls, name: str) -> bool:
        return cls._allowed_component(name) and (
            Path(name).suffix.lower() in cls.SOURCE_EXTENSIONS or name.lower() in cls.SOURCE_NAMES
        )

    def _relative_path(self, path: str) -> Path:
        if not isinstance(path, str) or not path or len(path) > 500 or "\x00" in path:
            raise RepositoryError("A nonempty relative source path is required (max 500 chars).")
        normalized = path.replace("\\", "/")
        parts = normalized.split("/")
        if (
            normalized.startswith("/")
            or PureWindowsPath(path).drive
            or any(not self._allowed_component(part) for part in parts)
            or any(part.lower() in self.EXCLUDED_DIRECTORIES for part in parts[:-1])
            or len(parts) - 1 > self.MAX_DEPTH
        ):
            raise RepositoryError("Path is outside the allowed repository source view.")
        if not self._source_name(parts[-1]):
            raise RepositoryError("Only supported source and documentation files may be read.")
        return Path(*parts)

    def _checked_path(self, relative: Path) -> tuple[Path, os.stat_result]:
        current = self.root
        try:
            # Recheck the root too, in case the directory was replaced after construction.
            if self._is_link(current.lstat()) or current.resolve(strict=True) != self.root:
                raise RepositoryError("Repository root changed or became a link.")
            for part in relative.parts:
                current = current / part
                info = current.lstat()
                if self._is_link(info):
                    raise RepositoryError("Links and reparse points are not readable.")
            resolved = current.resolve(strict=True)
            if not resolved.is_relative_to(self.root):
                raise RepositoryError("Path escapes the repository root.")
            if not stat.S_ISREG(info.st_mode):
                raise RepositoryError("Only regular source files may be read.")
            if info.st_size > self.MAX_FILE_BYTES:
                raise RepositoryError("File exceeds the 256 KiB source limit.")
            return resolved, info
        except (OSError, RuntimeError):
            raise RepositoryError("Source file is missing or inaccessible.") from None

    def _read_source(self, relative: Path) -> tuple[list[str], str, int]:
        path, before = self._checked_path(relative)
        descriptor: int | None = None
        try:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                opened = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_size > self.MAX_FILE_BYTES
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                ):
                    raise RepositoryError("Source file changed while opening it.")
                raw = handle.read(self.MAX_FILE_BYTES + 1)
                after_read = os.fstat(handle.fileno())
            _, after = self._checked_path(relative)
            if (
                len(raw) > self.MAX_FILE_BYTES
                or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
                or opened.st_mtime_ns != after_read.st_mtime_ns
                or opened.st_size != after_read.st_size
                or after_read.st_mtime_ns != after.st_mtime_ns
            ):
                raise RepositoryError("Source file changed while reading it.")
            text = raw.decode("utf-8")
            if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
                raise RepositoryError("Binary files are not readable.")
        except (OSError, UnicodeError):
            raise RepositoryError("Source must be an accessible UTF-8 text file.") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n") if normalized else []
        if lines and lines[-1] == "":
            lines.pop()
        return lines, hashlib.sha256(raw).hexdigest(), len(raw)

    def _scan(self) -> tuple[list[Path], bool]:
        """Enumerate at most MAX_ENTRIES entries, without following any links."""
        files: list[Path] = []
        pending = [(self.root, 0)]
        inspected = 0
        truncated = False
        while pending:
            directory, depth = pending.pop()
            children: list[Path] = []
            try:
                # No source is returned by traversal; _read_source revalidates every path.
                if self._is_link(directory.lstat()):
                    continue
                if not directory.resolve(strict=True).is_relative_to(self.root):
                    continue
                with os.scandir(directory) as entries:
                    for entry in entries:
                        inspected += 1
                        if inspected > self.MAX_ENTRIES:
                            return sorted(files, key=lambda p: p.as_posix()), True
                        if not self._allowed_component(entry.name):
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError:
                            truncated = True
                            continue
                        if self._is_link(info):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if entry.name.lower() in self.EXCLUDED_DIRECTORIES:
                                continue
                            if depth >= self.MAX_DEPTH:
                                truncated = True
                            else:
                                children.append(Path(entry.path))
                        elif (
                            stat.S_ISREG(info.st_mode)
                            and info.st_size <= self.MAX_FILE_BYTES
                            and self._source_name(entry.name)
                        ):
                            if len(files) >= self.MAX_FILES:
                                return sorted(files, key=lambda p: p.as_posix()), True
                            relative = Path(entry.path).relative_to(self.root)
                            # Apply precisely the same filename policy as direct reads.
                            try:
                                files.append(self._relative_path(relative.as_posix()))
                            except RepositoryError:
                                continue
            except (OSError, RuntimeError):
                truncated = True
                continue
            pending.extend((child, depth + 1) for child in sorted(children, reverse=True))
        return sorted(files, key=lambda p: p.as_posix()), truncated

    def _read_candidates(self):
        candidates, truncated = self._scan()
        scanned_bytes = 0
        for relative in candidates:
            try:
                _, info = self._checked_path(relative)
                if scanned_bytes + info.st_size > self.MAX_SCAN_BYTES:
                    yield None, None, True
                    return
                # Invalid/binary candidates consume the scan budget too.
                scanned_bytes += info.st_size
                lines, _, _ = self._read_source(relative)
            except RepositoryError:
                continue
            yield relative, lines, False
        yield None, None, truncated

    def list_files(self) -> dict[str, Any]:
        files = []
        truncated = False
        for relative, _, partial in self._read_candidates():
            truncated = truncated or partial
            if relative is not None:
                files.append(relative.as_posix())
        return {"files": files, "truncated": truncated}

    def search(self, query: str, limit: int = 8) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > self.MAX_QUERY_LENGTH:
            raise RepositoryError("Search query must contain 1 to 200 characters.")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise RepositoryError("Search limit must be an integer from 1 to 20.")
        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        truncated = False
        for relative, lines, partial in self._read_candidates():
            truncated = truncated or partial
            if relative is None:
                continue
            for number, line in enumerate(lines, start=1):
                index = line.casefold().find(needle)
                if index < 0:
                    continue
                if len(matches) == limit:
                    return {"matches": matches, "truncated": True}
                snippet = line
                if len(line) > self.MAX_MATCH_TEXT:
                    start = max(0, index - 100)
                    end = start + self.MAX_MATCH_TEXT
                    snippet = ("…" if start else "") + line[start:end]
                    snippet += "…" if end < len(line) else ""
                matches.append({"path": relative.as_posix(), "line": number, "text": snippet})
        return {"matches": matches, "truncated": truncated}

    def read_file(self, path: str, start_line: int = 1, end_line: int = 80) -> dict[str, Any]:
        if (
            type(start_line) is not int
            or type(end_line) is not int
            or start_line < 1
            or end_line < start_line
            or end_line - start_line + 1 > self.MAX_LINES
        ):
            raise RepositoryError("Read range must use 1-based integers and at most 120 lines.")
        relative = self._relative_path(path)
        lines, digest, _ = self._read_source(relative)
        if start_line > len(lines):
            raise RepositoryError("Start line is beyond the end of the source file.")
        actual_end = min(end_line, len(lines))
        content = "\n".join(lines[start_line - 1 : actual_end])
        if len(content.encode("utf-8")) > self.MAX_READ_BYTES:
            raise RepositoryError("Selected lines exceed 32 KiB; request a smaller range.")
        return {
            "path": relative.as_posix(),
            "start_line": start_line,
            "end_line": actual_end,
            "content": content,
            "sha256": digest,
        }

    def verify_evidence(self, evidence: dict) -> dict[str, Any]:
        if not isinstance(evidence, dict):
            return {"valid": False, "reason": "Evidence must be an object."}
        if not all(key in evidence for key in ("path", "start_line", "end_line", "quote")):
            return {"valid": False, "reason": "Evidence is missing required fields."}
        if not isinstance(evidence["quote"], str):
            return {"valid": False, "reason": "Evidence quote must be a string."}
        try:
            excerpt = self.read_file(evidence["path"], evidence["start_line"], evidence["end_line"])
        except RepositoryError as exc:
            return {"valid": False, "reason": str(exc)}
        if excerpt["end_line"] != evidence["end_line"]:
            return {"valid": False, "reason": "Evidence range extends beyond the source file."}
        if excerpt["content"] != evidence["quote"]:
            return {"valid": False, "reason": "Quote does not exactly match the referenced lines."}
        return {"valid": True, "reason": "Quote exactly matches the referenced source lines."}

    def dispatch(self, name: str, arguments: dict) -> dict[str, Any]:
        if not isinstance(arguments, dict) or any(not isinstance(key, str) for key in arguments):
            raise RepositoryError("Tool arguments must be an object with string keys.")
        allowed = {
            "list_files": (set(), set()),
            "search": ({"query", "limit"}, {"query"}),
            "read_file": ({"path", "start_line", "end_line"}, {"path"}),
        }
        if not isinstance(name, str) or name not in allowed:
            raise RepositoryError("Unknown repository tool.")
        permitted, required = allowed[name]
        if not required.issubset(arguments) or not set(arguments).issubset(permitted):
            raise RepositoryError("Tool arguments contain missing or unsupported fields.")
        return getattr(self, name)(**arguments)
