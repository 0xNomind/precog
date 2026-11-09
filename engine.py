"""Precog learning engine.

Pure logic with no Hermes imports, so every rule here is unit-testable:

* parse shell commands into simple-command segments (quotes, heredocs and
  redirects aware) without losing the character offsets needed to rewrite them;
* read a ``terminal`` tool result and decide whether it failed, and why
  (an *error signature*);
* pair a failed command with the later command that fixed it;
* turn that pair into a *lesson* (swap a program, add a prefix, swap a flag,
  swap a path, or a plain hint);
* apply lessons to a new command string.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------

_SEPARATORS = ("&&", "||", ";;", "|&", ";", "|", "&", "(", ")")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = {"sudo", "time", "nohup", "exec", "env", "command", "builtin", "noglob"}
_STATEMENT_STARTS = {"^", "\n", ";", "&&", "||"}


@dataclass
class Segment:
    """One simple command inside a (possibly compound) shell command."""

    start: int  # offset of the segment in the full command string
    end: int
    sep_before: str  # separator that precedes the segment ("^" for start)
    raw: str
    tokens: list[str] = field(default_factory=list)  # program first, wrappers removed
    env_names: list[str] = field(default_factory=list)
    prog_start: int = -1  # offset of the program token in the full string
    prog_end: int = -1

    @property
    def program(self) -> str:
        return self.tokens[0] if self.tokens else ""

    @property
    def prog_base(self) -> str:
        return os.path.basename(self.program)

    @property
    def args(self) -> list[str]:
        return self.tokens[1:]

    @property
    def at_statement_start(self) -> bool:
        return self.sep_before in _STATEMENT_STARTS


def _scan_spans(cmd: str) -> list[tuple[int, int, str]]:
    """Split *cmd* into (start, end, separator_before) spans.

    Quotes, backslash escapes, comments, ``2>&1``-style redirects and heredoc
    bodies are respected; heredoc bodies are dropped entirely because they are
    data, not commands.
    """
    spans: list[tuple[int, int, str]] = []
    n = len(cmd)
    i = start = 0
    sep = "^"
    quote: Optional[str] = None
    pending_heredocs: list[str] = []

    def close(end: int, next_sep: str, next_start: int) -> None:
        nonlocal start, sep
        if cmd[start:end].strip():
            spans.append((start, end, sep))
        sep = next_sep
        start = next_start

    while i < n:
        c = cmd[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c == "#" and (i == 0 or cmd[i - 1].isspace()):
            j = cmd.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "<" and cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
            m = _HEREDOC_RE.match(cmd, i)
            if m:
                pending_heredocs.append(m.group(2))
                i = m.end()
                continue
        if c == "\n":
            j = i + 1
            while pending_heredocs:
                delim = pending_heredocs.pop(0)
                while j < n:
                    k = cmd.find("\n", j)
                    line = cmd[j : n if k < 0 else k]
                    j = n if k < 0 else k + 1
                    if line.strip() == delim:
                        break
            close(i, "\n", j)
            i = j
            continue
        matched = None
        for op in _SEPARATORS:
            if cmd.startswith(op, i):
                matched = op
                break
        if matched:
            # "2>&1", "&>file", ">&2" are redirects, not separators.
            if matched == "&" and (
                (i > 0 and cmd[i - 1] in "<>") or (i + 1 < n and cmd[i + 1] == ">")
            ):
                i += 1
                continue
            close(i, matched, i + len(matched))
            i += len(matched)
            continue
        i += 1
    close(n, "", n)
    return spans


_WRAPPER_PREFIX_RE = re.compile(
    r"\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|\S*)\s+)"
    r"|(?:sudo|env)(?:\s+-\S+)*\s+"
    r"|(?:time|nohup|exec|builtin|noglob)\s+)*"
)


_REDIRECT_RE = re.compile(r"^(?:\d*|&)(?:>>?|<<?|>&|<&)(.*)$")


def _strip_redirects(tokens: list[str]) -> list[str]:
    """Drop ``2>&1``, ``>out.log``, ``> out.log``, ``<in`` style tokens (they are not arguments)."""
    out: list[str] = []
    skip_next = False
    for t in tokens:
        if skip_next:
            skip_next = False
            continue
        m = _REDIRECT_RE.match(t)
        if m:
            if not m.group(1):  # operator on its own: the target is the next token
                skip_next = True
            continue
        out.append(t)
    return out


def parse_command(cmd: str) -> list[Segment]:
    """Parse a shell command into simple-command segments."""
    segments: list[Segment] = []
    if not isinstance(cmd, str) or not cmd.strip():
        return segments
    for start, end, sep in _scan_spans(cmd):
        raw = cmd[start:end]
        try:
            toks = shlex.split(raw, comments=True, posix=True)
        except ValueError:
            toks = raw.split()
        env_names: list[str] = []
        k = 0
        while k < len(toks):
            t = toks[k]
            if _ASSIGN_RE.match(t):
                env_names.append(t.split("=", 1)[0])
                k += 1
                continue
            if t in _WRAPPERS and not (t == "command" and k + 1 < len(toks) and toks[k + 1].startswith("-")):
                k += 1
                while k < len(toks) and toks[k].startswith("-") and toks[k - 1] in ("sudo", "env"):
                    k += 1
                continue
            break
        tokens = _strip_redirects(toks[k:])
        seg = Segment(start=start, end=end, sep_before=sep, raw=raw, tokens=tokens, env_names=env_names)
        if tokens:
            m = _WRAPPER_PREFIX_RE.match(raw)
            off = m.end() if m else 0
            # The program token as written (may be quoted); find it from `off`.
            rest = raw[off:]
            pm = re.match(r"('[^']*'|\"[^\"]*\"|[^\s;&|()<>]+)", rest)
            if pm:
                seg.prog_start = start + off + pm.start()
                seg.prog_end = start + off + pm.end()
        segments.append(seg)
    return segments


# ---------------------------------------------------------------------------
# Results and error signatures
# ---------------------------------------------------------------------------

_PROBE_PROGRAMS = {
    "grep", "egrep", "fgrep", "rg", "ag", "diff", "cmp", "test", "[", "[[",
    "which", "type", "command", "pgrep", "pidof", "ls", "stat", "file", "find",
    "head", "tail", "cat", "echo", "printf", "pwd", "true", "false", "wc",
    "git-diff", "sleep", "jq",
}
_INSPECTION_PROGRAMS = _PROBE_PROGRAMS | {"cd", "env", "printenv", "uname", "whoami", "date", "tree", "du", "df"}


@dataclass
class ErrorSig:
    kind: str
    subject: str
    line: str

    def key(self) -> str:
        return f"{self.kind}:{self.subject}" if self.subject else self.kind


_ERR_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("no_module", re.compile(r"No module named ['\"]?([A-Za-z0-9_.\-]+)")),
    ("cmd_not_found", re.compile(r"command not found:\s*([^\s:'\"]+)")),
    ("cmd_not_found", re.compile(r"([^\s:'\"]+): (?:command )?not found")),
    ("cmd_not_found", re.compile(r"'([^']+)' is not recognized as an internal or external command")),
    ("wrong_pm", re.compile(r"(?:[Uu]se|configured to use|requires)\s+\"?(pnpm|yarn|bun|npm)\b")),
    ("wrong_pm", re.compile(r"ERR_PNPM_[A-Z_]+|Usage Error: This project is configured to use (\w+)")),
    ("python_version", re.compile(
        r"(?:unsupported operand type\(s\) for \|: 'type'|requires a different Python|"
        r"Requires-Python|python_requires|Python (?:3\.\d+|>=\s*3\.\d+) (?:or (?:newer|later|higher) )?is required|"
        r"match statement|SyntaxError: invalid syntax.*:=)")),
    ("bad_flag", re.compile(
        r"(?:unrecognized arguments?|unknown (?:option|flag|argument)|invalid option|no such option|"
        r"illegal option)[:\s]+['\"]?(-{1,2}[A-Za-z0-9][\w\-]*)")),
    ("no_such_file", re.compile(r"\[Errno 2\] No such file or directory: ['\"]([^'\"]+)['\"]")),
    ("no_such_file", re.compile(r"can't open file ['\"]([^'\"]+)['\"]")),
    ("no_such_file", re.compile(r"ENOENT: no such file or directory, \w+ ['\"]([^'\"]+)['\"]")),
    ("no_such_file", re.compile(r"(?:cannot access|cd:|open|stat) ['\"]?([^'\":\s]+)['\"]?: No such file or directory")),
    ("no_such_file", re.compile(r"([^\s:'\"]+): No such file or directory")),
    ("permission", re.compile(r"Permission denied(?:: ['\"]?([^'\"\s]+))?")),
    ("missing_env", re.compile(r"(?:environment variable|env var)[^A-Z]{0,20}([A-Z][A-Z0-9_]{2,})[^\n]{0,30}(?:not set|is required|missing)")),
    ("missing_env", re.compile(r"KeyError: ['\"]([A-Z][A-Z0-9_]{2,})['\"]")),
    ("http", re.compile(r"HTTP Error (\d{3})")),
    ("npm", re.compile(r"npm (?:ERR!|error) code (\w+)")),
]
_EXC_RE = re.compile(r"^\s*([A-Za-z_][\w.]*(?:Error|Exception|Exit))\b:?\s*(.*)$")
_GENERIC_RE = re.compile(r"\b(error|fatal|failed|cannot|could not|not found)\b", re.I)


def _clip(text: str, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def error_signature(output: str) -> ErrorSig:
    """Find the most informative error in a command's output."""
    text = output or ""
    tail = "\n".join(text.splitlines()[-80:])
    for kind, rx in _ERR_RULES:
        m = rx.search(tail)
        if m:
            subject = next((g for g in m.groups() if g), "") if m.groups() else ""
            if kind == "no_module":
                subject = subject.split(".")[0]
            line_start = tail.rfind("\n", 0, m.start()) + 1
            line_end = tail.find("\n", m.end())
            line = tail[line_start : len(tail) if line_end < 0 else line_end]
            return ErrorSig(kind, subject, _clip(line))
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = _EXC_RE.match(ln)
        if m:
            return ErrorSig("exception", m.group(1).split(".")[-1], _clip(ln))
    for ln in reversed(lines):
        if _GENERIC_RE.search(ln):
            return ErrorSig("error", "", _clip(ln))
    return ErrorSig("exit", "", _clip(lines[-1]) if lines else "")


@dataclass
class TerminalOutcome:
    known: bool  # False when the result is not a finished foreground run
    failed: bool
    exit_code: Optional[int]
    output: str
    sig: Optional[ErrorSig] = None
    blamed: Optional[int] = None  # index of the segment blamed for the failure
    blames: list = field(default_factory=list)  # every (segment index, ErrorSig) implicated
    masked: bool = False  # exit code 0, but a pipe / || chain hid an environment error


def _loads(result) -> dict:
    if isinstance(result, dict):
        return result
    try:
        val = json.loads(result) if isinstance(result, str) else {}
        return val if isinstance(val, dict) else {"output": str(val)}
    except (TypeError, ValueError):
        return {"output": str(result or "")}


def read_terminal_result(command: str, result, segments: Optional[list[Segment]] = None) -> TerminalOutcome:
    """Decide whether a ``terminal`` call failed and which segment caused it."""
    data = _loads(result)
    output = str(data.get("output") or data.get("stdout") or "")
    if data.get("stderr"):
        output = f"{output}\n{data['stderr']}"
    code = data.get("exit_code", data.get("returncode"))
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    err = data.get("error")
    if code is None and not err:
        # Background processes, approvals denied, etc. are not outcomes we learn from.
        status = str(data.get("status") or "").lower()
        if status in ("ok", "success", "completed") and output:
            return TerminalOutcome(True, False, 0, output)
        return TerminalOutcome(False, False, None, output)
    failed = bool(err) or (code not in (0, None))
    segs = segments if segments is not None else parse_command(command)
    runnable = [x for x in segs if x.tokens]
    text = f"{output}\n{err or ''}"
    if not failed:
        # `a 2>&1 | tail` and `a || b` hide a's failure behind a 0 exit code.
        if len(runnable) > 1:
            blames = _env_blames(segs, text)
            if blames:
                idx, sig = blames[0]
                return TerminalOutcome(True, True, code, output, sig, idx, blames, masked=True)
        return TerminalOutcome(True, False, code, output)
    sig = error_signature(text)
    blamed = blame_segment(segs, sig)
    if blamed is not None and code == 1 and not err:
        seg = segs[blamed]
        if seg.prog_base in _PROBE_PROGRAMS and sig.kind in ("exit", "error"):
            return TerminalOutcome(True, False, code, output)  # grep found nothing, etc.
    blames = [(blamed, sig)] if blamed is not None else []
    if len(runnable) > 1:
        for pair in _env_blames(segs, text):
            if all(pair[0] != b[0] for b in blames):
                blames.append(pair)
    return TerminalOutcome(True, True, code, output, sig, blamed, blames)


def all_env_signatures(output: str) -> list[ErrorSig]:
    """Every distinct environment-class error in *output* (cascades report several)."""
    tail = "\n".join((output or "").splitlines()[-120:])
    found: dict[str, ErrorSig] = {}
    for kind, rx in _ERR_RULES:
        if kind not in ENV_KINDS:
            continue
        for m in rx.finditer(tail):
            subject = next((g for g in m.groups() if g), "") if m.groups() else ""
            if kind == "no_module":
                subject = subject.split(".")[0]
            sig = ErrorSig(kind, subject, _clip(tail[tail.rfind("\n", 0, m.start()) + 1 : (tail.find("\n", m.end()) + 1 or len(tail) + 1) - 1]))
            found.setdefault(sig.key(), sig)
    return list(found.values())


def _env_blames(segs: list[Segment], output: str) -> list[tuple[int, "ErrorSig"]]:
    blames: list[tuple[int, ErrorSig]] = []
    for sig in all_env_signatures(output):
        idxs = _segments_for(segs, sig)
        for idx in idxs:
            if all(idx != b[0] for b in blames):
                blames.append((idx, sig))
    return blames


def _segments_for(segs: list[Segment], sig: "ErrorSig") -> list[int]:
    """Segments a signature points at (all of them, for a cascade like `python -m X || python3 -m X`)."""
    if not sig.subject:
        return []
    subj = sig.subject
    hits = []
    for i, s in enumerate(segs):
        if not s.tokens or _is_inspection(s):
            continue
        if sig.kind == "no_module":
            if _runs_python_module(s, subj):
                hits.append(i)
            continue
        if sig.kind == "cmd_not_found":
            if s.prog_base == os.path.basename(subj) or s.program == subj:
                hits.append(i)
        elif any(subj == t or subj == os.path.basename(t) or t.endswith("/" + subj) for t in s.tokens):
            hits.append(i)
    return hits


# Errors that describe the *environment* (wrong interpreter, missing tool, wrong
# package manager, bad flag, missing path) rather than the work being done.
# Only these teach lessons: a failing test or a bug in a script is not a mistake
# in how a command was run, and fixing the code must not become a "lesson".
ENV_KINDS = frozenset({
    "no_module", "cmd_not_found", "wrong_pm", "python_version", "bad_flag",
    "no_such_file", "permission", "missing_env",
})
_TEST_REPORT_RE = re.compile(
    r"(?m)^(?:FAILED |ERROR |=+ .*\b(?:failed|errors?)\b.*=+$|E\s{3}|Ran \d+ tests?|"
    r"Tests?:\s+\d+ failed|\d+ failing\b|test result: FAILED|--- FAIL:)"
)
_COLLECTION_RE = re.compile(r"ImportError while importing test module|ERROR collecting")


def _project_packages(scope: str) -> set[str]:
    names: set[str] = set()
    if not scope or not os.path.isdir(scope):
        return names
    for base in (scope, os.path.join(scope, "src")):
        try:
            for entry in os.listdir(base):
                p = os.path.join(base, entry)
                if os.path.isdir(p) and os.path.exists(os.path.join(p, "__init__.py")):
                    names.add(entry)
                elif entry.endswith(".py"):
                    names.add(entry[:-3])
        except OSError:
            continue
    return names


def is_learnable(sig: Optional[ErrorSig], output: str, scope: str = "") -> bool:
    """True when a failure is about how the command was run, not about the code it ran."""
    if sig is None or sig.kind not in ENV_KINDS:
        return False
    if sig.kind == "no_module":
        if _COLLECTION_RE.search(output or ""):
            return False  # pytest could not import the project's own test module: a code problem
        if sig.subject in _project_packages(scope):
            return False  # the project's own package: a code problem, not an environment one
    elif _TEST_REPORT_RE.search(output or ""):
        return False  # the tool ran and reported failing work
    return True


_PIP_QUERIES = {"show", "list", "freeze", "check", "--version", "-V", "inspect", "index"}


def _is_inspection(seg: Segment) -> bool:
    if seg.prog_base in _INSPECTION_PROGRAMS:
        return True
    # `pip show x`, `python -m pip list`, `uv pip list`: asking, not doing.
    args = seg.args
    if _family(seg.prog_base) == "pip" and args and args[0] in _PIP_QUERIES:
        return True
    if contains_in_order(args, ["-m", "pip"]):
        i = args.index("pip")
        return i + 1 < len(args) and args[i + 1] in _PIP_QUERIES
    return False


def _runs_python_module(seg: Segment, module: str) -> bool:
    """Could this segment have raised `No module named <module>`?"""
    if _is_inspection(seg):
        return False
    if contains_in_order(seg.args, ["-m", module]) or seg.prog_base == module:
        return True
    # A Python interpreter running a script, `-c` code or a heredoc can import anything.
    return _family(seg.prog_base) == "python" and "-m" not in seg.args


def blame_segment(segs: list[Segment], sig: Optional[ErrorSig]) -> Optional[int]:
    """Pick the segment that most likely produced the error."""
    runnable = [i for i, s in enumerate(segs) if s.tokens]
    if not runnable:
        return None
    if sig and sig.subject:
        subj = sig.subject
        if sig.kind == "no_module":
            hits = [i for i in runnable if _runs_python_module(segs[i], subj)]
            if hits:
                return hits[-1]
        for i in runnable:
            s = segs[i]
            if sig.kind == "cmd_not_found" and (s.prog_base == os.path.basename(subj) or s.program == subj):
                return i
        for i in reversed(runnable):
            s = segs[i]
            if any(subj == t or subj == os.path.basename(t) or t.endswith("/" + subj) for t in s.tokens):
                return i
    # Otherwise: last segment that is not pure inspection.
    for i in reversed(runnable):
        if segs[i].prog_base not in _INSPECTION_PROGRAMS:
            return i
    return runnable[-1]


# ---------------------------------------------------------------------------
# Similarity, pairing and lesson extraction
# ---------------------------------------------------------------------------


def _arg_tokens(seg: Segment) -> list[str]:
    return [t for t in seg.args if t]


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / max(1, len(sa | sb))


def is_probe_command(segs: list[Segment]) -> bool:
    runnable = [s for s in segs if s.tokens]
    return bool(runnable) and all(s.prog_base in _INSPECTION_PROGRAMS for s in runnable)


def key_args_for(seg: Segment, sig: Optional[ErrorSig]) -> list[str]:
    """The arguments that identify *what* a command does (``-m pytest``, ``test``)."""
    args = seg.args
    if sig and sig.kind in ("cmd_not_found", "wrong_pm", "python_version"):
        return []
    if "-m" in args:
        i = args.index("-m")
        if i + 1 < len(args):
            return ["-m", args[i + 1]]
    if sig and sig.kind == "no_module" and sig.subject in args:
        return [sig.subject]
    # First subcommand-looking word (``test``, ``build``, ``run``), skipping flag values.
    for i, a in enumerate(args):
        if i > 0 and args[i - 1].startswith("-") and "=" not in args[i - 1]:
            continue
        if _SUBCOMMAND_RE.match(a):
            return [a]
    return []


_SUBCOMMAND_RE = re.compile(r"^[a-z][a-z0-9_:\-]{0,30}$")


def contains_in_order(haystack: list[str], needle: list[str]) -> bool:
    if not needle:
        return True
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


_SAFE_PROGRAM_RE = re.compile(r"^[~A-Za-z0-9_./+@:\-]+$")
_VENV_ACTIVATE_RE = re.compile(r"^\s*(?:source|\.)\s+\S*activate\S*\s*$|^\s*conda\s+activate\b")
_FAMILY_RE = re.compile(r"^(python|pip|node|ruby|perl)(?:\d+(?:\.\d+)*)?$")


def _family(prog: str) -> str:
    m = _FAMILY_RE.match(prog or "")
    return m.group(1) if m else prog
_ACTIVATION_RE = re.compile(
    r"^\s*(?:(?:source|\.)\s+\S*activate\S*"
    r"|cd\s+\S+"
    r"|(?:nvm|fnm)\s+use\b[^\n;&|]*"
    r"|conda\s+activate\s+\S+"
    r"|pyenv\s+(?:shell|local)\s+\S+"
    r"|corepack\s+enable\b[^\n;&|]*)\s*$"
)


@dataclass
class Lesson:
    kind: str  # swap_program | prefix | swap_flag | swap_path | hint
    program: str  # program base the lesson applies to ("" = any)
    key_args: list[str]
    data: dict
    hint: str
    error_key: str
    fail_cmd: str
    fix_cmd: str

    def fingerprint(self) -> str:
        return json.dumps(
            [self.kind, self.program, self.key_args, self.data, self.error_key if self.kind == "hint" else ""],
            sort_keys=True,
        )


def find_fix_segment(fail_seg: Segment, sig: Optional[ErrorSig], fix_segs: list[Segment]) -> Optional[int]:
    """Find the segment of a later, successful command that fixed *fail_seg*."""
    best, best_score = None, 0.0
    f_args = _arg_tokens(fail_seg)
    subject = sig.subject if sig else ""
    f_stdin = _is_stdin_script(fail_seg)
    for i, s in enumerate(fix_segs):
        if not s.tokens or _is_inspection(s):
            continue
        if f_stdin and _is_stdin_script(s) and s.program == fail_seg.program:
            continue  # two different heredoc scripts: same "command", different work
        sim = jaccard(f_args, _arg_tokens(s))
        score = sim
        if s.prog_base == fail_seg.prog_base or s.program == fail_seg.program:
            score += 0.15
        if subject and any(subject == t or t.endswith("/" + subject) for t in s.tokens):
            score += 0.35
        key = key_args_for(fail_seg, sig)
        if key and contains_in_order(s.args, key):
            score += 0.25
        if contains_in_order(s.args, ["-m", fail_seg.prog_base]):
            score += 0.3  # `pytest` -> `python -m pytest`
        # Same program, same shape, exactly one argument changed (a path or a flag).
        if s.prog_base == fail_seg.prog_base and len(s.args) == len(f_args) and f_args:
            diffs = sum(1 for a, b in zip(f_args, s.args) if a != b)
            if diffs == 1:
                score += 0.45
        if score > best_score:
            best, best_score = i, score
    return best if best_score >= 0.55 else None


def _is_stdin_script(seg: Segment) -> bool:
    return "<<" in seg.raw and (not seg.args or seg.args[-1] == "-" or "-" in seg.args)


def fix_excerpt(fix_segs: list[Segment], fix_idx: int) -> str:
    """The fixing segment plus the activation / cd steps right before it."""
    parts = [p.raw.strip() for p in fix_segs[:fix_idx] if p.tokens and _ACTIVATION_RE.match(p.raw)]
    parts.append(fix_segs[fix_idx].raw.strip().split("\n")[0])
    return " && ".join(parts)


def _hint_text(fail_seg: Segment, sig: Optional[ErrorSig], fix: str) -> str:
    err = f" ({sig.line})" if sig and sig.line else ""
    return f"`{_clip(fail_seg.raw.strip().splitlines()[0], 120)}` failed here{err}; `{_clip(fix, 160)}` worked."


def _effective_cwd(start: str, segs: list[Segment]) -> str:
    """The directory a segment list ends up in, following plain ``cd`` steps."""
    cur = start
    for seg in segs:
        if seg.prog_base == "cd" and seg.args:
            target = os.path.expanduser(seg.args[0])
            if target == "-":
                continue
            cur = os.path.normpath(target if os.path.isabs(target) or not cur else os.path.join(cur, target))
    return cur


def _is_relative_path(p: str) -> bool:
    return "/" in p and not p.startswith(("/", "~"))


def _outside(program: str, base: str, project_cwd: str) -> bool:
    """True when *program* is a path that resolves outside the project it was learned in."""
    if "/" not in program:
        return False
    path = os.path.expanduser(program)
    if not os.path.isabs(path):
        path = os.path.join(base or project_cwd or ".", path)
    root = project_root(project_cwd) if project_cwd else ""
    if not root:
        return False
    # Resolve the folder, not the file: a venv interpreter is a symlink to a shared
    # Python elsewhere, but it still belongs to the project that owns the venv.
    path = os.path.join(os.path.realpath(os.path.dirname(path) or "."), os.path.basename(path))
    root = os.path.realpath(root)
    return not (path == root or path.startswith(root + os.sep))


def extract_lesson(fail_cmd: str, fail_segs: list[Segment], blamed: int, sig: Optional[ErrorSig],
                   fix_cmd: str, fix_segs: list[Segment], fix_idx: int, fix_cwd: str = "") -> Lesson:
    """Turn a (failure, fix) pair into the most specific lesson that explains it.

    ``fix_cwd`` is where the fixing command started; relative program paths and
    ``cd`` prefixes are anchored to it so a lesson never breaks in a subfolder.
    """
    f = fail_segs[blamed]
    s = fix_segs[fix_idx]
    base_dir = _effective_cwd(fix_cwd, fix_segs[:fix_idx]) if fix_cwd else ""
    key = key_args_for(f, sig)
    fix_part = fix_excerpt(fix_segs, fix_idx)
    hint = _hint_text(f, sig, fix_part)
    ekey = sig.key() if sig else "exit"
    # Keep the evidence to the parts that matter: the segment that failed and the
    # step(s) that fixed it, not the agent's whole multi-part command.
    base = dict(program=f.prog_base, key_args=key, hint=hint, error_key=ekey,
                fail_cmd=f.raw.strip().splitlines()[0] if f.raw.strip() else fail_cmd, fix_cmd=fix_part)

    f_args, s_args = _arg_tokens(f), _arg_tokens(s)
    # `pytest -q` fixed by `python -m pytest -q`: the failed program became a module.
    module_form = "-m" in s_args and f.prog_base in s_args and f.prog_base not in f_args
    same_job = (bool(key) and contains_in_order(s_args, key)) or module_form \
        or jaccard(f_args, s_args) >= 0.6 or (not key and s.prog_base == f.prog_base)

    # A. The fix added an activation / cd step before a command doing the same job:
    #    the environment was the problem, so the lesson is "do that step first".
    #    (If only a `cd` was added and the program changed, it is a program swap: D.)
    added_venv = any(_VENV_ACTIVATE_RE.match(p.raw) for p in fix_segs[:fix_idx] if p.tokens)
    same_program = s.prog_base == f.prog_base or module_form or (
        added_venv and _family(s.prog_base) == _family(f.prog_base))
    if fix_idx > 0 and s.at_statement_start and same_job and same_program:
        pre = [p for p in fix_segs[:fix_idx] if p.tokens]
        added = [p for p in pre if p.raw.strip() not in {x.raw.strip() for x in fail_segs}]
        if added and all(_ACTIVATION_RE.match(p.raw) for p in added):
            prefix = " && ".join(p.raw.strip() for p in added)
            data = {"prefix": prefix}
            if fix_cwd and any(p.prog_base == "cd" or _is_relative_path(" ".join(p.args)) for p in added):
                data["base"] = os.path.normpath(fix_cwd)
            return Lesson(kind="prefix", data=data, **base)

    def _swap(to: str, key_args: list) -> Lesson:
        data = {"from": f.program, "to": to}
        if _is_relative_path(to.split()[0]) and base_dir:
            data["base"] = base_dir
        return Lesson(kind="swap_program", data=data, **{**base, "key_args": key_args})

    # B. `pytest: command not found`, fixed by `.venv/bin/pytest …`: the same tool at a
    #    specific path. Arguments may differ freely; the program is what was wrong.
    if (sig and sig.kind == "cmd_not_found" and s.program != f.program
            and os.path.basename(s.program) == f.prog_base and _SAFE_PROGRAM_RE.match(s.program)):
        return _swap(s.program, [])

    # C. `pytest -q` fixed by `.venv/bin/python -m pytest -q`.
    if module_form and _SAFE_PROGRAM_RE.match(s.program or "") \
            and not _outside(s.program, base_dir or fix_cwd, fix_cwd):
        return _swap(f"{s.program} -m {f.prog_base}", [])

    # D. Same job, different program -> swap the program.
    if s.program != f.program and _SAFE_PROGRAM_RE.match(s.program or "") and not module_form:
        if jaccard(f_args, s_args) >= 0.6 or (key and contains_in_order(s_args, key)):
            # A missing module "fixed" by an interpreter that lives outside the project,
            # with nothing in the command naming the module, is too broad to automate.
            outside = _outside(s.program, base_dir or fix_cwd, fix_cwd)
            if not (sig and sig.kind == "no_module" and not key and outside):
                return _swap(s.program, key)

    # 3. A flag the installed tool does not accept, replaced by one it does.
    if sig and sig.kind == "bad_flag" and s.prog_base == f.prog_base:
        bad = sig.subject
        gone = [a for a in f_args if a.startswith("-") and a not in s_args]
        new = [a for a in s_args if a.startswith("-") and a not in f_args]
        bad_flag = bad if bad in gone else (gone[0] if len(gone) == 1 else "")
        if bad_flag and len(new) == 1:
            return Lesson(kind="swap_flag", data={"from": bad_flag, "to": new[0]}, **base)

    # 4. A path that does not exist, replaced by one that does.
    if sig and sig.kind == "no_such_file" and s.prog_base == f.prog_base:
        gone = [a for a in f_args if a not in s_args and ("/" in a or "." in a)]
        new = [a for a in s_args if a not in f_args and ("/" in a or "." in a)]
        if len(gone) == 1 and len(new) == 1 and _SAFE_PROGRAM_RE.match(new[0]):
            return Lesson(kind="swap_path", data={"from": gone[0], "to": new[0]}, **base)

    return Lesson(kind="hint", data={}, **base)


# ---------------------------------------------------------------------------
# Applying lessons
# ---------------------------------------------------------------------------


@dataclass
class Rewrite:
    command: str
    applied: list[int]  # lesson ids
    notes: list[str]


def _segment_matches(seg: Segment, program: str, key_args: list[str]) -> bool:
    if not seg.tokens:
        return False
    if program and seg.prog_base != program and seg.program != program:
        return False
    return contains_in_order(seg.args, key_args)


def _has_activation_before(segs: list[Segment], idx: int, prefix: str) -> bool:
    before = " ".join(s.raw for s in segs[:idx])
    if prefix.strip() in before:
        return True
    return bool(re.search(r"(?:source|\.)\s+\S*activate", before))


def _same_dir(a: str, b: str) -> bool:
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return a == b


def apply_lessons(command: str, lessons: list[dict], cwd: str = "") -> Rewrite:
    """Apply stored lessons (dicts from the store) to *command*.

    Edits are collected as character-range replacements and applied from the
    end of the string backwards, so offsets stay valid.
    """
    segs = parse_command(command)
    edits: list[tuple[int, int, str]] = []
    applied: list[int] = []
    notes: list[str] = []
    taken: set[int] = set()

    order = {"prefix": 0, "swap_program": 1, "swap_flag": 2, "swap_path": 3}
    for lesson in sorted(lessons, key=lambda l: order.get(l["kind"], 9)):
        kind = lesson["kind"]
        if kind not in order:
            continue
        data = lesson.get("data") or {}
        program = lesson.get("program") or ""
        key = lesson.get("key_args") or []
        for i, seg in enumerate(segs):
            if i in taken and kind in ("prefix", "swap_program"):
                continue
            if kind == "swap_path":
                src, dst = data.get("from"), data.get("to")
                if not src or not dst or seg.prog_base != program:
                    continue
                for m in re.finditer(r"(?<![\w./-])" + re.escape(src) + r"(?![\w./-])", seg.raw):
                    edits.append((seg.start + m.start(), seg.start + m.end(), dst))
                    applied.append(lesson["id"])
                    notes.append(f"path `{src}` → `{dst}`")
                    break
                continue
            if not _segment_matches(seg, program, key):
                continue
            if kind == "swap_program":
                src, dst = data.get("from"), data.get("to")
                if seg.program == dst or seg.prog_start < 0:
                    continue
                anchor = data.get("base")
                if anchor and _is_relative_path(dst or "") and not (cwd and _same_dir(cwd, anchor)):
                    dst = os.path.join(anchor, dst)  # learned in another folder: use the absolute path
                    if seg.program == dst:
                        continue
                if program and seg.prog_base != os.path.basename(src or ""):
                    continue
                edits.append((seg.prog_start, seg.prog_end, dst))
                taken.add(i)
                applied.append(lesson["id"])
                notes.append(f"`{seg.program}` → `{dst}`")
            elif kind == "prefix":
                prefix = data.get("prefix", "")
                if not prefix or not seg.at_statement_start or _has_activation_before(segs, i, prefix):
                    continue
                if data.get("base") and not (cwd and _same_dir(cwd, data["base"])):
                    continue  # a relative prefix only makes sense where it was learned
                at = seg.start + (len(seg.raw) - len(seg.raw.lstrip()))
                edits.append((at, at, prefix + " && "))
                taken.add(i)
                applied.append(lesson["id"])
                notes.append(f"added `{prefix}` first")
            elif kind == "swap_flag":
                src, dst = data.get("from"), data.get("to")
                for m in re.finditer(r"(?<![\w-])" + re.escape(src or "") + r"(?![\w-])", seg.raw):
                    edits.append((seg.start + m.start(), seg.start + m.end(), dst))
                    applied.append(lesson["id"])
                    notes.append(f"flag `{src}` → `{dst}`")
                    break

    if not edits:
        return Rewrite(command, [], [])
    out = command
    for start, end, text in sorted(set(edits), key=lambda e: (e[0], e[1]), reverse=True):
        out = out[:start] + text + out[end:]
    seen: list[int] = []
    for lid in applied:
        if lid not in seen:
            seen.append(lid)
    return Rewrite(out, seen, notes)


def normalized_signature(seg: Segment) -> str:
    """Stable identity of a simple command for the known-failure tripwire."""
    return " ".join([seg.prog_base] + seg.args[:8])


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

_ROOT_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "Gemfile", "pom.xml",
                 "build.gradle", "composer.json", "Package.swift", "mix.exs", "deno.json")


def project_root(cwd: str) -> str:
    """The project a working directory belongs to (nearest .git, else nearest marker)."""
    if not cwd:
        return ""
    try:
        path = os.path.realpath(os.path.expanduser(cwd))
    except OSError:
        return cwd
    first_marker = ""
    cur = path
    for _ in range(40):
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        if not first_marker and any(os.path.exists(os.path.join(cur, m)) for m in _ROOT_MARKERS):
            first_marker = cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return first_marker or path
