import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine as E  # noqa: E402


def term(output, code):
    return json.dumps({"output": output, "exit_code": code, "error": None})


# --- parsing ---------------------------------------------------------------


def test_parse_compound_and_offsets():
    cmd = "cd app && FOO=1 sudo python3 -m pytest -q 2>&1 | tail -5"
    segs = E.parse_command(cmd)
    assert [s.prog_base for s in segs] == ["cd", "python3", "tail"]
    py = segs[1]
    assert py.env_names == ["FOO"]
    assert cmd[py.prog_start:py.prog_end] == "python3"
    assert py.args[:2] == ["-m", "pytest"]
    assert segs[2].sep_before == "|"


def test_heredoc_body_is_not_commands():
    cmd = "python3 - <<'PY'\nimport os; print(1) && rm -rf /\nPY\necho done"
    segs = E.parse_command(cmd)
    assert [s.prog_base for s in segs] == ["python3", "echo"]


def test_redirect_ampersand_not_separator():
    segs = E.parse_command("make build >out.log 2>&1 &")
    assert [s.prog_base for s in segs] == ["make"]


# --- results ---------------------------------------------------------------


def test_no_module_failure_is_detected_and_blamed():
    cmd = "python3 -m pytest tests -q"
    out = E.read_terminal_result(cmd, term("/usr/bin/python3: No module named pytest", 1))
    assert out.known and out.failed
    assert out.sig.kind == "no_module" and out.sig.subject == "pytest"
    assert out.blamed == 0


def test_grep_no_match_is_not_a_failure():
    out = E.read_terminal_result("grep -rn TODO src", term("", 1))
    assert out.known and not out.failed


def test_background_result_is_unknown():
    out = E.read_terminal_result("npm run dev", json.dumps({"session_id": "p1", "pid": 42}))
    assert not out.known


def test_python_version_signature():
    sig = E.error_signature("TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'")
    assert sig.kind == "python_version"


def test_wrong_package_manager_signature():
    sig = E.error_signature('Usage Error: This project is configured to use pnpm')
    assert sig.kind == "wrong_pm"


# One minified or base64 line must not stall the agent thread. Before the rules were
# anchored, cmd_not_found and no_such_file took ~10 s each on a 50k-character token.
_LONG_TOKENS = {
    "letters": "A" * 50_000,
    "base64": ("aGVsbG8gd29ybGQ" * 3400)[:50_000],
    "dots": "a." * 25_000,
    "equals": "=" * 50_000,
    "colons": "a:" * 25_000,
}


def test_long_single_token_output_is_fast():
    import time

    for label, token in _LONG_TOKENS.items():
        start = time.perf_counter()
        E.error_signature(token)
        E.all_env_signatures(token)
        E.read_terminal_result("node build.js", term(token, 1))
        E.read_terminal_result("node build.js 2>&1 | tail -1", term(token, 0))  # masked-failure path
        E.is_learnable(E.ErrorSig("no_such_file", "x", ""), token)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"{label}: {elapsed:.2f}s"


def test_rules_still_read_the_line_around_a_long_token():
    junk = "x" * 50_000
    sig = E.error_signature(f"{junk}\nbash: foo: command not found")
    assert (sig.kind, sig.subject) == ("cmd_not_found", "foo")
    sig = E.error_signature(f"cat: {junk}: No such file or directory")
    assert sig.kind == "no_such_file"  # the cap keeps the line's head and tail
    sig = E.error_signature("ls: build/out.min.js: No such file or directory")
    assert (sig.kind, sig.subject) == ("no_such_file", "build/out.min.js")


# --- pairing + lessons -----------------------------------------------------


def _pair(fail_cmd, fail_out, fix_cmd):
    fsegs = E.parse_command(fail_cmd)
    res = E.read_terminal_result(fail_cmd, term(fail_out, 1), fsegs)
    assert res.failed
    xsegs = E.parse_command(fix_cmd)
    idx = E.find_fix_segment(fsegs[res.blamed], res.sig, xsegs)
    assert idx is not None, "fix segment not found"
    return E.extract_lesson(fail_cmd, fsegs, res.blamed, res.sig, fix_cmd, xsegs, idx)


def test_swap_program_interpreter():
    lesson = _pair(
        "python3 -m pytest tests -q",
        "/usr/bin/python3: No module named pytest",
        "/opt/python3.11/bin/python3.11 -m pytest tests -q",
    )
    assert lesson.kind == "swap_program"
    assert lesson.data == {"from": "python3", "to": "/opt/python3.11/bin/python3.11"}
    assert lesson.key_args == ["-m", "pytest"]


def test_prefix_activation_lesson():
    lesson = _pair(
        "python -m pytest -q",
        "No module named pytest",
        ". .venv/bin/activate && python -m pytest -q",
    )
    assert lesson.kind == "prefix"
    assert lesson.data["prefix"] == ". .venv/bin/activate"


def test_wrong_pm_swaps_program_for_every_subcommand():
    lesson = _pair("npm test", "Usage Error: This project is configured to use pnpm", "pnpm test")
    assert lesson.kind == "swap_program" and lesson.key_args == []


def test_bad_flag_lesson():
    lesson = _pair("ffmpeg -i in.mp4 --crf 23 out.mp4", "Unrecognized option '--crf'.\nunknown option: --crf",
                   "ffmpeg -i in.mp4 -crf 23 out.mp4")
    assert lesson.kind == "swap_flag"
    assert lesson.data == {"from": "--crf", "to": "-crf"}


def test_path_lesson():
    lesson = _pair("python3 scripts/seed.py --dry-run",
                   "python3: can't open file '/w/app/scripts/seed.py': [Errno 2] No such file or directory",
                   "python3 tools/seed.py --dry-run")
    assert lesson.kind == "swap_path"
    assert lesson.data == {"from": "scripts/seed.py", "to": "tools/seed.py"}
    rw = E.apply_lessons("python3 scripts/seed.py", [_as_row(lesson)])
    assert rw.command == "python3 tools/seed.py"


def test_inspection_commands_are_never_fixes():
    fsegs = E.parse_command("cat config/settings.yaml")
    res = E.read_terminal_result("cat config/settings.yaml",
                                 term("cat: config/settings.yaml: No such file or directory", 1), fsegs)
    assert E.find_fix_segment(fsegs[res.blamed], res.sig, E.parse_command("cat config/settings.yml")) is None


def test_unrelated_success_is_not_a_fix():
    fsegs = E.parse_command("python3 -m pytest tests -q")
    res = E.read_terminal_result("python3 -m pytest tests -q", term("No module named pytest", 1), fsegs)
    idx = E.find_fix_segment(fsegs[res.blamed], res.sig, E.parse_command("git status"))
    assert idx is None


# --- applying --------------------------------------------------------------


def _as_row(lesson, lid=1):
    return {"id": lid, "kind": lesson.kind, "program": lesson.program, "key_args": lesson.key_args, "data": lesson.data}


def test_apply_swap_program_only_on_matching_segment():
    lesson = _pair("python3 -m pytest tests -q", "No module named pytest", "python3.11 -m pytest tests -q")
    rw = E.apply_lessons("python3 scripts/seed.py && python3 -m pytest -x", [_as_row(lesson)])
    assert rw.command == "python3 scripts/seed.py && python3.11 -m pytest -x"
    assert rw.applied == [1]


def test_apply_prefix_is_idempotent_when_already_activated():
    lesson = _pair("python -m pytest -q", "No module named pytest", ". .venv/bin/activate && python -m pytest -q")
    row = _as_row(lesson)
    assert E.apply_lessons("python -m pytest tests/unit", [row]).command == \
        ". .venv/bin/activate && python -m pytest tests/unit"
    already = "source .venv/bin/activate; python -m pytest"
    assert E.apply_lessons(already, [row]).command == already


def test_apply_does_not_touch_heredoc_body():
    lesson = _pair("npm test", "This project is configured to use pnpm", "pnpm test")
    cmd = "cat <<'EOF' > notes.md\nrun npm test before pushing\nEOF\nnpm test"
    rw = E.apply_lessons(cmd, [_as_row(lesson)])
    assert rw.command == "cat <<'EOF' > notes.md\nrun npm test before pushing\nEOF\npnpm test"


def test_apply_swap_flag():
    lesson = _pair("ffmpeg -i a.mp4 --crf 23 b.mp4", "unknown option: --crf", "ffmpeg -i a.mp4 -crf 23 b.mp4")
    rw = E.apply_lessons("ffmpeg -y -i x.mov --crf 28 y.mp4", [_as_row(lesson)])
    assert rw.command == "ffmpeg -y -i x.mov -crf 28 y.mp4"


def test_project_root(tmp_path):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "packages" / "api"
    sub.mkdir(parents=True)
    (sub / "package.json").write_text("{}")
    assert E.project_root(str(sub)) == os.path.realpath(str(tmp_path))
