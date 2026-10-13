"""Repository boundary tests; runnable with pytest or stdlib unittest discovery."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from triageflow.repository import Repository, RepositoryError


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.repo = Repository(self.root)

    def put(self, path: str, content: str | bytes = "hello\n") -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return target

    def test_rejects_invalid_roots(self):
        file = self.put("main.py")
        for root in (self.root / "missing", file, None):
            with self.subTest(root=root), self.assertRaises(RepositoryError):
                Repository(root)

    def test_read_normalizes_line_endings_preserves_whitespace_and_hashes_full_file(self):
        raw = b"first\r\n  second  \r\nthird\r\n"
        self.put("src/main.py", raw)
        result = self.repo.read_file("src\\main.py", 2, 80)
        self.assertEqual(
            result,
            {
                "path": "src/main.py",
                "start_line": 2,
                "end_line": 3,
                "content": "  second  \nthird",
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
        )

    def test_read_range_validation(self):
        self.put("main.py", "\n".join(f"line {i}" for i in range(130)))
        for start, end in ((0, 1), (2, 1), (1, 121), (True, 2), (1, 2.0), (131, 140)):
            with self.subTest(start=start, end=end), self.assertRaises(RepositoryError):
                self.repo.read_file("main.py", start, end)
        self.assertEqual(self.repo.read_file("main.py", 1, 120)["end_line"], 120)

    def test_empty_file_can_be_listed_but_has_no_citable_line(self):
        self.put("empty.py", "")
        self.assertIn("empty.py", self.repo.list_files()["files"])
        with self.assertRaises(RepositoryError):
            self.repo.read_file("empty.py")

    def test_path_traversal_absolute_paths_and_windows_aliases_are_rejected(self):
        self.put("main.py")
        paths = (
            "../main.py",
            "src/../../main.py",
            "src\\..\\main.py",
            "/main.py",
            "C:\\main.py",
            "C:main.py",
            "\\\\server\\share\\main.py",
            "main.py:secret",
            "./main.py",
            "src//main.py",
            "main.py\x00",
            "main.py ",
            "CON.py",
            "LPT1.py",
        )
        for path in paths:
            with self.subTest(path=path), self.assertRaises(RepositoryError):
                self.repo.read_file(path)

    def test_hidden_secrets_build_output_and_nonsource_are_excluded(self):
        blocked = (
            ".env",
            ".env.example",
            ".git/config.py",
            ".hidden.py",
            "secrets.json",
            "credentials.yaml",
            "prod.env.json",
            "api-token.txt",
            "private-key.txt",
            "secrets/main.py",
            "node_modules/pkg/index.js",
            "dist/app.js",
            "key.pem",
            "image.png",
            "archive.zip",
            "file.exe",
        )
        for path in blocked:
            self.put(path)
        self.put("src/main.py")
        self.put("README.md")
        self.put("Dockerfile")
        self.assertEqual(
            self.repo.list_files(),
            {"files": ["Dockerfile", "README.md", "src/main.py"], "truncated": False},
        )
        for path in blocked:
            with self.subTest(path=path), self.assertRaises(RepositoryError):
                self.repo.read_file(path)

    def test_binary_invalid_utf8_oversize_and_nonregular_files_are_rejected(self):
        self.put("binary.py", b"hello\x00world")
        self.put("invalid.py", b"hello\xffworld")
        self.put("large.py", b"x" * (self.repo.MAX_FILE_BYTES + 1))
        (self.root / "directory.py").mkdir()
        self.assertEqual(self.repo.list_files()["files"], [])
        for path in ("binary.py", "invalid.py", "large.py", "directory.py"):
            with self.subTest(path=path), self.assertRaises(RepositoryError):
                self.repo.read_file(path)

    def test_read_output_limit(self):
        self.put("long.py", "a" * (self.repo.MAX_READ_BYTES + 1))
        with self.assertRaises(RepositoryError):
            self.repo.read_file("long.py", 1, 1)

    def test_literal_case_insensitive_search_with_precise_limit(self):
        self.put("src/main.py", "A.*[B]\na.*[b]\nother\n")
        self.put("README.md", "A random B\n")
        result = self.repo.search("a.*[b]", 2)
        self.assertEqual(
            result,
            {
                "matches": [
                    {"path": "src/main.py", "line": 1, "text": "A.*[B]"},
                    {"path": "src/main.py", "line": 2, "text": "a.*[b]"},
                ],
                "truncated": False,
            },
        )
        self.assertTrue(self.repo.search("a.*[b]", 1)["truncated"])
        self.assertEqual(self.repo.search("absent"), {"matches": [], "truncated": False})

    def test_search_validates_arguments_and_bounds_match_text(self):
        self.put("main.py", "x" * 1_000 + "needle" + "x" * 1_000)
        match = self.repo.search("needle")["matches"][0]
        self.assertIn("needle", match["text"])
        self.assertLessEqual(len(match["text"]), self.repo.MAX_MATCH_TEXT + 2)
        for query in ("", "  ", "a" * 201, None, 123):
            with self.subTest(query=query), self.assertRaises(RepositoryError):
                self.repo.search(query)
        for limit in (0, 21, True, 1.0, "2"):
            with self.subTest(limit=limit), self.assertRaises(RepositoryError):
                self.repo.search("x", limit)

    def test_evidence_is_exact_including_whitespace_and_range(self):
        self.put("main.py", "def check():\n    return False  \n")
        evidence = {
            "path": "main.py",
            "start_line": 1,
            "end_line": 2,
            "quote": "def check():\n    return False  ",
        }
        self.assertTrue(self.repo.verify_evidence(evidence)["valid"])
        for update in (
            {"quote": evidence["quote"].strip()},
            {"end_line": 3},
            {"start_line": True},
            {"quote": None},
            {"path": "../main.py"},
            {"path": "missing.py"},
        ):
            with self.subTest(update=update):
                self.assertFalse(self.repo.verify_evidence({**evidence, **update})["valid"])
        self.assertFalse(self.repo.verify_evidence({})["valid"])
        self.assertFalse(self.repo.verify_evidence(None)["valid"])
        self.put("main.py", "def check():\n    return True  \n")
        self.assertFalse(self.repo.verify_evidence(evidence)["valid"])

    def test_dispatch_rejects_unknown_tools_and_unexpected_arguments(self):
        self.put("main.py")
        self.assertEqual(self.repo.dispatch("list_files", {}), self.repo.list_files())
        self.assertEqual(
            self.repo.dispatch("search", {"query": "hello"}), self.repo.search("hello")
        )
        self.assertEqual(
            self.repo.dispatch("read_file", {"path": "main.py"}), self.repo.read_file("main.py")
        )
        for name, arguments in (
            ("__dict__", {}),
            ("write_file", {}),
            ("list_files", {"root": "elsewhere"}),
            ("search", {}),
            ("read_file", {"path": "main.py", "execute": True}),
            ("search", []),
            ("search", {1: "hello"}),
            (None, {}),
        ):
            with self.subTest(name=name, arguments=arguments), self.assertRaises(RepositoryError):
                self.repo.dispatch(name, arguments)

    def test_candidate_count_and_entry_count_are_bounded(self):
        for index in range(6):
            self.put(f"file{index}.py", "needle")
        with patch.object(Repository, "MAX_FILES", 2):
            result = self.repo.list_files()
            self.assertEqual(len(result["files"]), 2)
            self.assertTrue(result["truncated"])
        with patch.object(Repository, "MAX_ENTRIES", 2):
            result = self.repo.search("needle", 20)
            self.assertLessEqual(len(result["matches"]), 2)
            self.assertTrue(result["truncated"])

    def test_scan_byte_budget_counts_invalid_files_too(self):
        self.put("a.py", "hello")
        self.put("b.py", b"\x00" * 5)
        self.put("c.py", "hello")
        with patch.object(Repository, "MAX_SCAN_BYTES", 10):
            self.assertEqual(self.repo.list_files(), {"files": ["a.py"], "truncated": True})
            result = self.repo.search("hello")
            self.assertEqual(len(result["matches"]), 1)
            self.assertTrue(result["truncated"])

    def test_depth_limit_is_applied_to_scan_and_direct_reads(self):
        self.put("a/b/main.py")
        with patch.object(Repository, "MAX_DEPTH", 1):
            self.assertEqual(self.repo.list_files(), {"files": [], "truncated": True})
            with self.assertRaises(RepositoryError):
                self.repo.read_file("a/b/main.py")

    def test_windows_reparse_attribute_is_treated_as_a_link(self):
        info = SimpleNamespace(st_mode=0o100644, st_file_attributes=0x400)
        self.assertTrue(Repository._is_link(info))

    def test_symlink_files_and_directories_are_never_followed(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "private.py").write_text("private source", encoding="utf-8")
        link = self.root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
            (self.root / "alias.py").symlink_to(outside / "private.py")
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks is unavailable in this environment.")
        self.assertEqual(self.repo.list_files()["files"], [])
        for path in ("linked/private.py", "alias.py"):
            with self.subTest(path=path), self.assertRaises(RepositoryError):
                self.repo.read_file(path)
        with self.assertRaises(RepositoryError):
            Repository(link)

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_windows_junction_escape_is_blocked(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "private.py").write_text("private source", encoding="utf-8")
        junction = self.root / "junction"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest("This environment cannot create directory junctions.")
        self.addCleanup(lambda: os.rmdir(junction) if junction.exists() else None)
        self.assertEqual(self.repo.list_files()["files"], [])
        with self.assertRaises(RepositoryError):
            self.repo.read_file("junction/private.py")
        with self.assertRaises(RepositoryError):
            Repository(junction)


if __name__ == "__main__":
    unittest.main()
