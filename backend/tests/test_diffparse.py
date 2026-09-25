"""RFC 15.3: a multi-hunk patch, the `\\ No newline` marker, omitted counts."""

from __future__ import annotations

from docket.collectors.diffparse import parse_patch

MULTI_HUNK = """@@ -1,4 +1,6 @@
 import os
-old_one = 1
+new_one = 1
+new_two = 2
 keep_me = 3
 tail = 4
@@ -20,3 +22,4 @@ def later():
 context
+added_here = 5
-dropped = 6
 more_context
"""


def test_multi_hunk_patch():
    hunks = parse_patch("provisioning/sync.py", MULTI_HUNK)
    assert len(hunks) == 2

    first, second = hunks
    assert first.hunk_id == "provisioning/sync.py#0"
    assert second.hunk_id == "provisioning/sync.py#1"
    assert first.path == second.path == "provisioning/sync.py"

    assert (first.old_start, first.old_lines, first.new_start, first.new_lines) == (1, 4, 1, 6)
    assert [(l.line_no, l.text) for l in first.added] == [(2, "new_one = 1"), (3, "new_two = 2")]
    assert first.removed_count == 1

    assert (second.old_start, second.old_lines, second.new_start, second.new_lines) == (20, 3, 22, 4)
    assert [(l.line_no, l.text) for l in second.added] == [(23, "added_here = 5")]
    assert second.removed_count == 1


def test_no_newline_marker_is_skipped_and_does_not_advance_the_counter():
    patch = (
        "@@ -1,2 +1,2 @@\n"
        " first\n"
        "-second\n"
        "\\ No newline at end of file\n"
        "+second_new\n"
        "\\ No newline at end of file\n"
    )
    hunks = parse_patch("a.py", patch)
    assert len(hunks) == 1
    assert [(l.line_no, l.text) for l in hunks[0].added] == [(2, "second_new")]
    assert hunks[0].removed_count == 1


def test_omitted_counts_default_to_one():
    hunks = parse_patch("a.py", "@@ -5 +7 @@\n-gone\n+here\n")
    assert len(hunks) == 1
    h = hunks[0]
    assert (h.old_start, h.old_lines, h.new_start, h.new_lines) == (5, 1, 7, 1)
    assert [(l.line_no, l.text) for l in h.added] == [(7, "here")]
    assert h.removed_count == 1

    # Only one side omitted.
    h2 = parse_patch("a.py", "@@ -5,3 +7 @@\n+here\n")[0]
    assert (h2.old_lines, h2.new_lines) == (3, 1)


def test_a_new_file_patch():
    hunks = parse_patch("new.py", "@@ -0,0 +1,3 @@\n+a = 1\n+b = 2\n+c = 3\n")
    assert [(l.line_no, l.text) for l in hunks[0].added] == [(1, "a = 1"), (2, "b = 2"), (3, "c = 3")]
    assert hunks[0].removed_count == 0
    assert hunks[0].old_start == 0 and hunks[0].old_lines == 0


def test_empty_context_line_advances_the_counter():
    hunks = parse_patch("a.py", "@@ -1,3 +1,4 @@\n a\n\n+added\n b\n")
    assert [(l.line_no, l.text) for l in hunks[0].added] == [(3, "added")]


def test_section_heading_after_the_at_at_is_ignored():
    hunks = parse_patch("a.py", "@@ -1,1 +1,2 @@ def thing(self) -> int:\n context\n+added\n")
    assert hunks[0].new_start == 1
    assert [l.line_no for l in hunks[0].added] == [2]


def test_absent_or_empty_patch_gives_no_hunks():
    assert parse_patch("a.py", None) == []
    assert parse_patch("a.py", "") == []
    # Junk before any header is skipped rather than raising.
    assert parse_patch("a.py", "nonsense\nmore nonsense\n") == []
    # A full-file header is tolerated if one ever arrives.
    hunks = parse_patch("a.py", "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+x\n")
    assert len(hunks) == 1 and [l.text for l in hunks[0].added] == ["x"]


def test_added_lines_keep_leading_whitespace_and_plus_signs():
    hunks = parse_patch("a.py", "@@ -1,1 +1,3 @@\n context\n+    indented = 1\n++double_plus\n")
    assert [l.text for l in hunks[0].added] == ["    indented = 1", "+double_plus"]
