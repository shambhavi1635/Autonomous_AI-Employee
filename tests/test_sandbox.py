"""The sandbox boundary, and the file tools' behaviour at their edges.

These are the tests that would catch the worst possible regression in
this project: the agent reaching a file outside its box. The traversal
cases are not hypothetical -- the agent reads documents it did not write,
and a document can contain instructions aimed at the model.
"""

from __future__ import annotations

import pytest

from tools.files import FileAccessError, list_files, read_file, write_file


@pytest.fixture
def box(sandbox):
    (sandbox / "notes.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (sandbox / "sub").mkdir()
    (sandbox / "sub" / "deep.txt").write_text("nested", encoding="utf-8")
    return sandbox


class TestSandboxBoundary:
    @pytest.mark.parametrize(
        "escape",
        [
            "../secrets.txt",
            "../../../Windows/System32/drivers/etc/hosts",
            "notes/../../../../secrets.txt",   # only visible after resolving
            "..",
            "sub/../../outside.txt",
        ],
    )
    def test_traversal_is_refused(self, box, escape):
        with pytest.raises(FileAccessError, match="outside the sandbox"):
            read_file(escape)

    @pytest.mark.parametrize("absolute", ["C:/Windows/win.ini", "/etc/passwd"])
    def test_absolute_paths_are_refused(self, box, absolute):
        with pytest.raises(FileAccessError, match="outside the sandbox"):
            read_file(absolute)

    def test_writes_cannot_escape_either(self, box):
        with pytest.raises(FileAccessError, match="outside the sandbox"):
            write_file("../escaped.txt", "nope")

    def test_resolution_happens_before_the_check(self, box):
        # The ordering is the whole mechanism. A check on the raw string
        # would pass this, because it starts with an innocent folder name
        # and the traversal only appears once the path is normalised.
        with pytest.raises(FileAccessError):
            read_file("sub/../../../../../../etc/passwd")


class TestReadFile:
    def test_reads_text(self, box):
        assert read_file("notes.txt") == "one\ntwo\nthree\n"

    def test_missing_file_says_so(self, box):
        with pytest.raises(FileAccessError, match="No such file"):
            read_file("nope.txt")

    def test_directory_is_not_a_file(self, box):
        with pytest.raises(FileAccessError, match="is a directory"):
            read_file("sub")

    def test_binary_file_explains_itself(self, box):
        # The agent reads this message and can recover from it -- that is
        # how the repair ladder learned to switch to read_pdf.
        (box / "thing.bin").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
        with pytest.raises(FileAccessError, match="not UTF-8"):
            read_file("thing.bin")

    def test_oversized_file_is_refused(self, box, monkeypatch):
        import tools.files

        monkeypatch.setattr(tools.files, "MAX_READ_BYTES", 10)
        (box / "big.txt").write_text("x" * 50, encoding="utf-8")
        with pytest.raises(FileAccessError, match="over the"):
            read_file("big.txt")


class TestListFiles:
    def test_default_is_recursive(self, box):
        # A plain '*' would hide a file the agent had just written into a
        # subfolder, and the agent would conclude the write had failed.
        assert list_files() == ["notes.txt", "sub/deep.txt"]

    def test_pattern_narrows(self, box):
        assert list_files("*.txt") == ["notes.txt"]

    def test_directories_are_not_listed(self, box):
        assert "sub" not in list_files()

    def test_empty_sandbox(self, sandbox):
        assert list_files() == []


class TestWriteFile:
    def test_writes_and_reports(self, box):
        assert "5 characters" in write_file("new.txt", "hello")
        assert read_file("new.txt") == "hello"

    def test_creates_parent_directories(self, box):
        write_file("a/b/c.txt", "deep")
        assert read_file("a/b/c.txt") == "deep"

    def test_overwrites(self, box):
        write_file("notes.txt", "replaced")
        assert read_file("notes.txt") == "replaced"

    def test_written_file_is_then_visible(self, box):
        write_file("out/result.txt", "x")
        assert "out/result.txt" in list_files()
