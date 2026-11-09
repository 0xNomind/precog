"""Drive Precog's hook callbacks the way Hermes does, against a temporary database."""

import importlib
import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(ROOT))
pkg = importlib.import_module(os.path.basename(ROOT))
runtime = importlib.import_module(pkg.__name__ + ".runtime")
store_mod = importlib.import_module(pkg.__name__ + ".store")


def term(output, code, cwd=None):
    body = {"output": output, "exit_code": code, "error": None}
    if cwd:
        body["cwd"] = cwd
    return json.dumps(body)


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".git").mkdir()
    return str(tmp_path)


@pytest.fixture(autouse=True)
def _no_broadcast(monkeypatch):
    monkeypatch.setattr(runtime, "_broadcast", lambda *a, **k: None)


@pytest.fixture
def pc(tmp_path, project, monkeypatch):
    # A brand-new Hermes session knows its working directory before any command runs.
    monkeypatch.setattr(runtime, "_agent_cwd", lambda: project)
    db = str(tmp_path / "precog.db")
    cfg = {}
    p = runtime.Precog(store_mod.Store(lambda: sqlite3.connect(db, check_same_thread=False)),
                       get_config=lambda k, d: cfg.get(k, d))
    p._cfg = cfg
    return p


def run(pc, session, cmd, output, code, cwd, call_id):
    """One terminal call through middleware -> pre_tool_call -> post -> transform."""
    args = {"command": cmd}
    ids = {"session_id": session, "task_id": session, "tool_call_id": call_id}
    mw = pc.on_tool_request(tool_name="terminal", args=args, **ids)
    if mw:
        args = mw["args"]
    pre = pc.on_pre_tool_call(tool_name="terminal", args=args, **ids)
    if pre and pre.get("action") == "block":
        return args["command"], None, pre
    result = term(output(args["command"]) if callable(output) else output,
                  code(args["command"]) if callable(code) else code, cwd)
    pc.on_post_tool_call(tool_name="terminal", args=args, result=result, **ids)
    shown = pc.on_transform_tool_result(tool_name="terminal", args=args, result=result, **ids)
    return args["command"], shown, None


def test_learn_then_prevent_across_sessions(pc, project):
    # Session 1: the classic mistake, then the fix.
    run(pc, "s1", "python3 -m pytest tests -q", "/usr/bin/python3: No module named pytest", 1, project, "c1")
    run(pc, "s1", "command -v python3.11", "/opt/py/bin/python3.11", 0, project, "c2")  # probe
    _, shown, _ = run(pc, "s1", "python3.11 -m pytest tests -q", "3 passed", 0, project, "c3")
    assert shown and "Learned lesson" in json.loads(shown)["precog"]
    lessons = pc.store.lessons_for(os.path.realpath(project))
    assert len(lessons) == 1 and lessons[0]["kind"] == "swap_program"

    # Session 2: fresh context, same mistake is rewritten before it runs.
    def out(cmd):
        return "3 passed" if cmd.startswith("python3.11") else "No module named pytest"

    def code(cmd):
        return 0 if cmd.startswith("python3.11") else 1

    ran, shown, _ = run(pc, "s2", "python3 -m pytest -x", out, code, project, "d1")
    assert ran == "python3.11 -m pytest -x"
    assert "rewrote this command" in json.loads(shown)["precog"]
    lesson = pc.store.lessons_for(os.path.realpath(project))[0]
    assert lesson["applied"] == 1 and lesson["saves"] == 1


def test_briefing_once_per_project(pc, project, monkeypatch):
    run(pc, "s1", "npm test", "Usage Error: This project is configured to use pnpm", 1, project, "c1")
    run(pc, "s1", "pnpm test", "ok", 0, project, "c2")
    first = pc.on_pre_llm_call(session_id="s9")
    assert first and "pnpm" in first["context"]
    assert pc.on_pre_llm_call(session_id="s9") is None


def test_hint_tripwire_blocks_once(pc, project):
    run(pc, "s1", "make deploy", "Error: environment variable AWS_PROFILE is not set", 1, project, "c1")
    run(pc, "s1", "make deploy-staging", "deployed", 0, project, "c2")
    kinds = [l["kind"] for l in pc.store.lessons_for(os.path.realpath(project))]
    assert kinds == ["hint"]
    _, _, blocked = run(pc, "s2", "make deploy", "Error", 1, project, "d1")
    assert blocked and "already failed" in blocked["message"] and "make deploy-staging" in blocked["message"]
    _, _, again = run(pc, "s2", "make deploy", "Error", 1, project, "d2")
    assert again is None  # second attempt goes through


def test_bad_lesson_turns_itself_off(pc, project):
    run(pc, "s1", "python3 -m pytest", "No module named pytest", 1, project, "c1")
    run(pc, "s1", "python3.11 -m pytest", "ok", 0, project, "c2")
    # The environment changed: python3.11 is gone now.
    for i in range(3):
        run(pc, f"s{i + 2}", "python3 -m pytest", "python3.11: command not found", 127, project, f"x{i}")
    lesson = pc.store.all_lessons()[0]
    assert lesson["enabled"] == 0


def test_modes(pc, project):
    run(pc, "s1", "npm test", "This project is configured to use pnpm", 1, project, "c1")
    run(pc, "s1", "pnpm test", "ok", 0, project, "c2")
    pc._cfg["mode"] = "suggest"
    ran, _, blocked = run(pc, "s2", "npm test", "x", 1, project, "d1")
    assert ran == "npm test" and blocked and "pnpm test" in blocked["message"]
    pc._cfg["mode"] = "learn"
    ran, _, blocked = run(pc, "s3", "npm test", "This project is configured to use pnpm", 1, project, "e1")
    assert ran == "npm test" and blocked is None


def test_other_projects_are_unaffected(pc, project, tmp_path_factory, monkeypatch):
    run(pc, "s1", "npm test", "This project is configured to use pnpm", 1, project, "c1")
    run(pc, "s1", "pnpm test", "ok", 0, project, "c2")
    other = tmp_path_factory.mktemp("other")
    (other / ".git").mkdir()
    monkeypatch.setattr(runtime, "_agent_cwd", lambda: str(other))
    ran, _, _ = run(pc, "s2", "npm test", "ok", 0, str(other), "d1")
    assert ran == "npm test"


def test_status_text(pc, project):
    run(pc, "s1", "npm test", "This project is configured to use pnpm", 1, project, "c1")
    run(pc, "s1", "pnpm test", "ok", 0, project, "c2")
    text = pc.status_text(os.path.realpath(project))
    assert "use `pnpm` instead of `npm`" in text


def test_failing_tests_are_not_mistakes(pc, project):
    out = "FAILED tests/test_x.py::test_a - assert 1 == 2\n=== 1 failed, 3 passed in 0.2s ==="
    run(pc, "s1", "python -m pytest -q", out, 1, project, "c1")
    run(pc, "s1", "python -m pytest -q", "4 passed", 0, project, "c2")
    assert pc.store.all_lessons() == []


def test_project_own_module_is_not_an_environment_error(pc, project):
    os.makedirs(os.path.join(project, "myapp"))
    open(os.path.join(project, "myapp", "__init__.py"), "w").close()
    run(pc, "s1", "python -m pytest tests/test_stop.py -q", "E   ModuleNotFoundError: No module named 'myapp.stop'", 1, project, "c1")
    run(pc, "s1", ". .venv/bin/activate && python -m pytest tests/test_stop.py -q", "1 passed", 0, project, "c2")
    assert pc.store.all_lessons() == []


def test_script_bug_is_not_learned(pc, project):
    run(pc, "s1", "python3 - <<'PY'\nd=[]\nd.get('x')\nPY", "AttributeError: 'list' object has no attribute 'get'", 1, project, "c1")
    run(pc, "s1", "python3 - <<'PY'\nd={}\nd.get('x')\nPY", "", 0, project, "c2")
    assert pc.store.all_lessons() == []


def test_relative_program_learned_after_cd_is_anchored(pc, project, monkeypatch):
    tools = os.path.join(project, "tools")
    os.makedirs(tools)
    run(pc, "s1", "python3 -m yamllint x.yml", "No module named yamllint", 1, project, "c1")
    run(pc, "s1", "cd tools && venv/bin/python -m yamllint ../x.yml", "ok", 0, project, "c2")
    lesson = pc.store.all_lessons()[0]
    assert lesson["kind"] == "swap_program" and lesson["data"]["base"].endswith("tools")
    ran, _, _ = run(pc, "s2", "python3 -m yamllint y.yml", "ok", 0, project, "d1")
    assert ran == f"{os.path.join(os.path.realpath(project) if lesson['data']['base'].startswith('/private') else project, 'tools', 'venv/bin/python')} -m yamllint y.yml" or ran.endswith("tools/venv/bin/python -m yamllint y.yml")


def test_masked_cascade_from_real_session(pc, project):
    """A cascade where every part failed but the trailing pipe made the exit code 0."""
    cascade = ("make test 2>&1 || pytest 2>&1 || python -m pytest 2>&1 || "
               "python3 -m pytest 2>&1 | tail -20")
    out = ("make: *** No rule to make target `test'.  Stop.\n"
           "/bin/bash: line 6: pytest: command not found\n"
           "/opt/env/bin/python: No module named pytest\n"
           "/usr/bin/python3: No module named pytest")
    run(pc, "s1", cascade, out, 0, project, "c1")
    run(pc, "s1", "ls .venv/bin/pytest", ".venv/bin/pytest", 0, project, "c2")
    run(pc, "s1", ".venv/bin/python -m pytest -q", "2 passed", 0, project, "c3")
    lessons = pc.store.lessons_for(os.path.realpath(project))
    swaps = {(l["data"]["from"], l["data"]["to"]) for l in lessons if l["kind"] == "swap_program"}
    assert ("python3", ".venv/bin/python") in swaps
    assert ("python", ".venv/bin/python") in swaps

    ran, shown, _ = run(pc, "s2", "python3 -m pytest -q 2>&1 | tail -5",
                        lambda c: "2 passed", lambda c: 0, project, "d1")
    assert ran.startswith(".venv/bin/python -m pytest -q") or ran.startswith(
        os.path.join(os.path.realpath(project), ".venv/bin/python"))
    assert "rewrote" in json.loads(shown)["precog"]


def test_command_not_found_fixed_by_venv_binary_from_real_session(pc, project):
    """`pytest -v` not found, fixed by `<abs>/.venv/bin/pytest -v <abs>/tests/`."""
    venv_pytest = os.path.join(project, ".venv/bin/pytest")
    run(pc, "s1", f"cd {project} && pytest -v", "/bin/bash: line 6: pytest: command not found", 127, project, "c1")
    run(pc, "s1", f"python3 -m pytest --version 2>&1; {venv_pytest} --version 2>&1",
        "/usr/bin/python3: No module named pytest\npytest 8.4.2", 0, project, "c2")
    run(pc, "s1", f"{venv_pytest} -v {project}/tests/", "2 passed", 0, project, "c3")
    swaps = [l for l in pc.store.all_lessons() if l["kind"] == "swap_program"]
    assert any(l["data"] == {"from": "pytest", "to": venv_pytest} for l in swaps)
    ran, _, _ = run(pc, "s2", "pytest -q tests", lambda c: "2 passed", lambda c: 0, project, "d1")
    assert ran == f"{venv_pytest} -q tests"


def test_venv_interpreter_symlink_counts_as_inside_project(pc, project):
    """python3.11 lacks httpx; the project's .venv/bin/python (a symlink) has it."""
    import engine as E  # noqa: F401  (pure module, path set by test_engine)
    bindir = os.path.join(project, ".venv", "bin")
    os.makedirs(bindir)
    os.symlink("/usr/bin/true", os.path.join(bindir, "python"))  # like uv: points outside
    run(pc, "s1", "/opt/py/bin/python3.11 /tmp/run_tests.py 2>&1",
        "ModuleNotFoundError: No module named 'httpx'", 1, project, "c1")
    run(pc, "s1", f"cd {project} && .venv/bin/python /tmp/run_tests.py 2>&1", "2 passed", 0, project, "c2")
    kinds = [l["kind"] for l in pc.store.all_lessons()]
    assert kinds == ["swap_program"]


def test_live_venv_activation_session(pc, project):
    """python3 / python / pytest all fail, activating the venv fixes it."""
    act = f". {project}/.venv/bin/activate"
    run(pc, "s1", "python3 -m pytest -q 2>&1", "/usr/bin/python3: No module named pytest", 1, project, "c1")
    run(pc, "s1", "python -m pytest --tb=no -q 2>&1", "/opt/hermes/python: No module named pytest", 1, project, "c2")
    run(pc, "s1", "pytest --tb=no -q 2>&1", "bash: pytest: command not found", 127, project, "c3")
    run(pc, "s1", f"{act} && python -m pytest --version 2>&1", "pytest 8.4.2", 0, project, "c4")
    lessons = pc.store.all_lessons()
    assert {l["kind"] for l in lessons} == {"prefix"}
    assert {l["program"] for l in lessons} == {"python3", "python", "pytest"}
    for cmd in ("python3 -m pytest -q", "pytest -x", "python -m pytest tests"):
        ran, _, _ = run(pc, "s2", cmd, lambda c: "2 passed", lambda c: 0, project, "d-" + cmd)
        assert ran == f"{act} && {cmd}", ran


def test_module_form_swap(pc, project):
    os.makedirs(os.path.join(project, ".venv", "bin"))
    run(pc, "s1", "pytest -q", "zsh: command not found: pytest", 127, project, "c1")
    run(pc, "s1", ".venv/bin/python -m pytest -q", "2 passed", 0, project, "c2")
    lesson = pc.store.all_lessons()[0]
    assert lesson["kind"] == "swap_program" and lesson["data"]["to"] == ".venv/bin/python -m pytest"
    ran, _, _ = run(pc, "s2", "pytest -x tests", lambda c: "ok", lambda c: 0, project, "d1")
    assert ran.endswith(".venv/bin/python -m pytest -x tests")


def test_probe_cascade_blames_only_python(pc, project):
    """`pip show pytest; which pytest; ls; python3 -m pytest --version` with exit code 0."""
    probe = ("pip show pytest 2>&1; echo ---; which pytest 2>&1; echo ---; ls -la .venv; echo ---; "
             "python3 -m pytest --version 2>&1")
    out = ("WARNING: Package(s) not found: pytest\n---\npytest not found\n---\ntotal 8\n---\n"
           "/usr/bin/python3: No module named pytest")
    run(pc, "s1", probe, out, 0, project, "c1")
    run(pc, "s1", "python --version; ls .venv/bin/python*; .venv/bin/python -m pytest --version",
        "Python 3.9\n.venv/bin/python\npytest 8.4.2", 0, project, "c2")
    progs = {l["program"] for l in pc.store.all_lessons()}
    assert "pip" not in progs and progs == {"python3"}


def test_following_the_briefing_counts_as_a_save(pc, project):
    run(pc, "s1", "python -m pytest -v", "No module named pytest", 1, project, "c1")
    run(pc, "s1", f"cd {project} && .venv/bin/python -m pytest -v", "2 passed", 0, project, "c2")
    assert pc.on_pre_llm_call(session_id="s2")  # briefing injected
    run(pc, "s2", "ls -d .venv && .venv/bin/python -m pytest -q 2>&1 | tail -20", "2 passed", 0, project, "d1")
    lesson = pc.store.all_lessons()[0]
    assert lesson["saves"] == 1 and pc.store.stats()["saves"] == 1
