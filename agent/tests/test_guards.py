"""Guard tests: the agent has no way to run code it didn't ship with.

These read the agent's own source (agent/*.py) as syntax trees and fail CI if a change adds
any of the things that would let a server, or anything else, make the agent run new code:

- eval / exec / compile / __import__ / importlib / pickle / marshal and friends
- a shell (shell=True, os.system, os.popen, os.exec*, os.spawn*, pty)
- subprocess anywhere but collectors._run, or a command that isn't a fixed list of strings
- writing files, changing sys.path, or fetching URLs (a self-update needs at least one)

They also feed hostile messages to parse_server_message, the one door the server has.
If one of these fails, the change probably breaks the promise in docs/trust-the-agent.md.
"""

import ast
import json
from pathlib import Path

import pytest

from agent import connection, protocol
from agent.protocol import parse_server_message

AGENT_DIR = Path(__file__).resolve().parents[1]
SOURCES = sorted(AGENT_DIR.glob("*.py"))


def tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def all_nodes():
    for path in SOURCES:
        for node in ast.walk(tree(path)):
            yield path.name, node


def dotted(node: ast.AST) -> str:
    """'os.system', 'self._get', 'eval' ... for a Name/Attribute chain ('' otherwise)."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def call_name(node: ast.Call) -> str:
    """The call's dotted name as written ('eval', 'os.system', 'self._get' ...)."""
    return dotted(node.func)


def aliases(module: ast.Module) -> dict[str, str]:
    """Local name -> full dotted name, from every import in the module.

    `import os as o` -> {"o": "os"}; `from subprocess import run as r` -> {"r":
    "subprocess.run"}. So a renamed import can't hide a forbidden call."""
    names: dict[str, str] = {}
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def resolve(name: str, names: dict[str, str]) -> str:
    head, _, rest = name.partition(".")
    full = names.get(head, head)
    return f"{full}.{rest}" if rest else full


class _Calls(ast.NodeVisitor):
    """Every call in a module with its resolved name and the function it sits in."""

    def __init__(self, names: dict[str, str]) -> None:
        self.names = names
        self.stack: list[str] = []
        self.found: list[tuple[ast.Call, str, str | None]] = []

    def _function(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.stack.append("<lambda>")
        self.generic_visit(node)
        self.stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        name = resolve(call_name(node), self.names)
        self.found.append((node, name, self.stack[-1] if self.stack else None))
        self.generic_visit(node)


def resolved_calls():
    """(file name, call node, resolved dotted name, enclosing function or None)."""
    for path in SOURCES:
        module = tree(path)
        visitor = _Calls(aliases(module))
        visitor.visit(module)
        for node, name, function in visitor.found:
            yield path.name, node, name, function


def test_the_sources_are_found():
    names = {p.name for p in SOURCES}
    assert {"collectors.py", "connection.py", "protocol.py", "docker_proxy.py"} <= names


FORBIDDEN_CALLS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "breakpoint",
    "os.system",
    "os.popen",
    "os.fork",
    "os.forkpty",
    "os.posix_spawn",
    "os.posix_spawnp",
    "os.open",
    "os.remove",
    "os.unlink",
    "os.rename",
    "os.replace",
    "os.chmod",
    "os.chown",
    "os.makedirs",
    "os.mkdir",
    "shutil.copy",
    "shutil.copyfile",
    "shutil.move",
    "shutil.rmtree",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.getoutput",
    "subprocess.getstatusoutput",
}


def test_no_code_execution_or_shell_calls():
    found = [
        f"{name}:{node.lineno} {resolved}"
        for name, node, resolved, _ in resolved_calls()
        if resolved in FORBIDDEN_CALLS
        or resolved.startswith(("os.exec", "os.spawn", "builtins."))
        or resolved.endswith((".write_text", ".write_bytes"))
    ]
    assert found == []


def test_renamed_imports_are_seen_through():
    """The alias resolution itself: these must all count as the forbidden call."""
    module = ast.parse(
        "import os as o\nfrom subprocess import run as r\nfrom os import system\n"
        "o.system('x')\nr(['x'])\nsystem('x')\n"
    )
    visitor = _Calls(aliases(module))
    visitor.visit(module)
    assert [name for _, name, _ in visitor.found] == ["os.system", "subprocess.run", "os.system"]


def test_no_builtins_tricks():
    """getattr(__builtins__, "eval"), globals()["exec"] and friends reach anything."""
    found = [
        f"{name}:{node.lineno} {node.id}"
        for name, node in all_nodes()
        if isinstance(node, ast.Name)
        and node.id in ("__builtins__", "globals", "vars", "locals", "__loader__", "__spec__")
    ]
    found += [
        f"{name}:{node.lineno} .{node.attr}"
        for name, node in all_nodes()
        if isinstance(node, ast.Attribute)
        and node.attr in ("__dict__", "__class__", "__subclasses__", "__globals__", "__code__")
    ]
    assert found == []


FORBIDDEN_MODULES = {
    "pickle",
    "marshal",
    "shelve",
    "importlib",
    "runpy",
    "ctypes",
    "code",
    "codeop",
    "zipimport",
    "pty",
    "builtins",
    "multiprocessing",
    "ftplib",
    "smtplib",
    "telnetlib",
    "xmlrpc",
    "socketserver",
    "http.server",
}


def test_getattr_only_with_a_fixed_name():
    """getattr(x, name) with a variable name could let data pick any attribute."""
    found = [
        f"{name}:{node.lineno}"
        for name, node in all_nodes()
        if isinstance(node, ast.Call)
        and call_name(node) in ("getattr", "setattr", "delattr")
        and not (len(node.args) >= 2 and isinstance(node.args[1], ast.Constant))
    ]
    assert found == []


def test_no_dangerous_imports():
    found = []
    for name, node in all_nodes():
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            if module in FORBIDDEN_MODULES or module.split(".")[0] in FORBIDDEN_MODULES:
                found.append(f"{name}:{node.lineno} {module}")
    assert found == []


def test_no_shell_true_anywhere():
    found = [
        f"{name}:{node.lineno}"
        for name, node in all_nodes()
        if isinstance(node, ast.keyword) and node.arg == "shell"
    ]
    assert found == []


def test_subprocess_only_in_collectors_run():
    """One place runs commands: collectors._run, through subprocess.run with a list.

    Every call in the whole tree counts (module level, classes, lambdas, async functions),
    with renamed imports resolved."""
    users = [
        (name, function, resolved)
        for name, _, resolved, function in resolved_calls()
        if resolved.startswith("subprocess.")
    ]
    assert users == [("collectors.py", "_run", "subprocess.run")]
    imports = set()
    for name, node in all_nodes():
        if isinstance(node, ast.Import) and any(
            a.name.split(".")[0] == "subprocess" for a in node.names
        ):
            imports.add(name)
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "subprocess":
            imports.add(f"{name} (from-import)")
    assert imports == {"collectors.py"}


def test_every_command_is_a_fixed_list():
    """Each _run([...]) is a list literal of string constants. The only variable is the unit
    name in run_unit_active, and its callers pass names from a literal tuple."""
    collectors = tree(AGENT_DIR / "collectors.py")
    calls = []
    for func in ast.walk(collectors):
        if not isinstance(func, ast.FunctionDef):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and call_name(node) == "_run":
                calls.append((func.name, node))
    assert len(calls) >= 8
    for func_name, node in calls:
        assert len(node.args) == 1 and not node.keywords, func_name
        argv = node.args[0]
        assert isinstance(argv, ast.List), f"{func_name}: argv must be a list literal"
        for element in argv.elts:
            if func_name == "run_unit_active" and isinstance(element, ast.Name):
                assert element.id == "unit"
                continue
            assert isinstance(element, ast.Constant) and isinstance(element.value, str), func_name
        assert isinstance(argv.elts[0], ast.Constant), f"{func_name}: the program is fixed"


def test_unit_names_come_from_a_literal():
    source = (AGENT_DIR / "collectors.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    callers = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call) and call_name(node) in ("_unit_is_active", "run_unit_active")
    ]
    # _unit_is_active(unit) inside the loop over a literal tuple, and run_unit_active(unit)
    # inside _unit_is_active: nothing else calls them.
    assert len(callers) == 2
    loops = [n for n in ast.walk(module) if isinstance(n, ast.For)]
    node_role_loop = [loop for loop in loops if isinstance(loop.iter, ast.Tuple)]
    assert node_role_loop, "collect_node_role must loop over a literal tuple of units"
    for element in node_role_loop[0].iter.elts:
        assert isinstance(element, ast.Tuple)
        assert all(isinstance(e, ast.Constant) for e in element.elts)


def test_files_are_only_opened_for_reading():
    bad = []
    for name, node in all_nodes():
        if not (isinstance(node, ast.Call) and call_name(node) == "open"):
            continue
        mode = node.args[1] if len(node.args) > 1 else None
        for kw in node.keywords:
            if kw.arg == "mode":
                mode = kw.value
        if mode is None:
            continue
        if not isinstance(mode, ast.Constant) or set(str(mode.value)) & set("wax+"):
            bad.append(f"{name}:{node.lineno}")
    assert bad == []


def test_sys_path_and_modules_are_never_changed():
    found = []
    for path in SOURCES:
        module = tree(path)
        names = aliases(module)
        for node in ast.walk(module):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in ("path", "modules", "meta_path", "path_hooks")
                and resolve(dotted(node.value), names) == "sys"
            ):
                found.append(f"{path.name}:{node.lineno}")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "sys"
                and any(a.name in ("path", "modules", "meta_path") for a in node.names)
            ):
                found.append(f"{path.name}:{node.lineno} from sys import")
    assert found == []


# Server-style calls that would make the agent accept connections.
LISTENING_CALLS = (".listen", ".accept", "start_server", "create_server", "start_unix_server")


def test_the_agent_never_listens():
    """Outbound only: no module accepts connections, except the Docker proxy, which accepts
    on the unix socket systemd hands it (it never creates a listening socket itself)."""
    found = []
    for name, node, resolved, _ in resolved_calls():
        if (
            resolved.endswith(LISTENING_CALLS)
            or resolved in ("websockets.serve",)
            or (resolved.startswith("websockets.") and resolved.endswith(".serve"))
        ):
            if name == "docker_proxy.py" and resolved.endswith(".accept"):
                continue
            found.append(f"{name}:{node.lineno} {resolved}")
    assert found == []


def test_bind_only_for_outgoing_probes():
    """network.py binds a UDP socket to port 0 to learn its own source address; nothing
    binds anywhere else."""
    found = [
        f"{name}:{node.lineno}"
        for name, node, resolved, _ in resolved_calls()
        if resolved.endswith(".bind") and name != "network.py"
    ]
    assert found == []
    binds = [
        node
        for name, node, resolved, _ in resolved_calls()
        if resolved.endswith(".bind") and name == "network.py"
    ]
    for node in binds:
        (arg,) = node.args
        assert isinstance(arg, ast.Tuple) and isinstance(arg.elts[1], ast.Constant)
        assert arg.elts[1].value == 0


def test_urls_are_fetched_only_by_the_opt_in_public_ip_check():
    """No module downloads anything; the one urlopen is network.py's public IP lookup, whose
    https:// URL comes from agent.toml and whose answer (64 bytes max) is parsed as an IP."""
    found = [
        f"{name}:{call_name(node)}"
        for name, node in all_nodes()
        if isinstance(node, ast.Call)
        and call_name(node).split(".")[-1] in ("urlopen", "urlretrieve")
    ]
    assert found == ["network.py:urllib.request.urlopen"]


def test_http_clients_only_where_expected():
    users = {
        name
        for name, node in all_nodes()
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and any(
            (alias.name if isinstance(node, ast.Import) else node.module or "").startswith(
                ("http.client", "urllib.request")
            )
            for alias in node.names
        )
    }
    assert users <= {"docker_api.py", "docker_proxy.py", "kube_api.py", "network.py"}


def test_no_self_update_words_in_the_code():
    """Belt and braces: no code path names an update or download of the agent itself."""
    words = ("self_update", "self-update", "auto_update", "autoupdate", "download_agent")
    for path in SOURCES:
        text = path.read_text(encoding="utf-8").lower()
        for word in words:
            assert word not in text, f"{path.name} mentions {word}"


def test_actions_config_is_never_read_by_code():
    """`actions` is reserved in agent.toml; until actions exist, no code may act on it."""
    readers = [
        f"{name}:{node.lineno}"
        for name, node in all_nodes()
        if isinstance(node, ast.Attribute) and node.attr == "actions" and name != "config.py"
    ]
    assert readers == []


# --- the server's one door: parse_server_message ------------------------------------------

HOSTILE = [
    {"type": "exec", "cmd": "id"},
    {"type": "run", "command": ["rm", "-rf", "/"]},
    {"type": "update", "url": "https://evil.example/agent.tar.gz"},
    {"type": "action", "name": "reboot"},
    {"type": "config", "server_url": "https://evil.example"},
    {"type": "collect", "cmd": "id"},
    {"type": "collect", "checks": ["shell"]},
    {"type": "welcome", "interval_seconds": 60, "enabled_checks": [], "script": "id"},
    {"type": "welcome", "interval_seconds": 60, "enabled_checks": ["updates; id"]},
    {"type": "welcome", "interval_seconds": 60, "enabled_checks": ["$(id)"]},
    {"type": "welcome", "interval_seconds": 60, "enabled_checks": ["../../etc"]},
    {"type": "welcome", "interval_seconds": 60, "enabled_checks": [{"name": "updates"}]},
    {"type": "welcome", "interval_seconds": "60; id", "enabled_checks": []},
    {"type": "welcome", "interval_seconds": 1, "enabled_checks": []},
    {"type": "welcome", "interval_seconds": 10**12, "enabled_checks": []},
    {"type": "cоllect"},  # Cyrillic "o"
    {"type": "COLLECT"},
    {"type": ["collect"]},
    {"type": None},
    {},
]


@pytest.mark.parametrize("message", HOSTILE, ids=range(len(HOSTILE)))
def test_hostile_server_messages_are_dropped(message):
    assert parse_server_message(json.dumps(message)) is None


@pytest.mark.parametrize(
    "raw",
    [
        '{"type": "collect", "type": "exec"}',  # duplicate key: the last one wins in json
        '{"type": "collect"} {"type": "exec"}',
        '{"type": "collect", "x": NaN}',
        "[" * 10_000 + "]" * 10_000,
        b'{"type": "collect"}',  # binary frame
        "﻿" + '{"type": "collect"}',
    ],
)
def test_tricky_frames_are_dropped(raw):
    assert parse_server_message(raw) is None


def test_only_two_message_types_exist():
    """welcome and collect are the whole protocol: there is no third branch to hide in."""
    module = tree(AGENT_DIR / "protocol.py")
    compared = {
        node.comparators[0].value
        for node in ast.walk(module)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "message_type"
        and isinstance(node.comparators[0], ast.Constant)
    }
    assert compared == {"welcome", "collect"}
    assert protocol.MAX_MESSAGE_BYTES <= 64 * 1024


def test_receive_loop_only_reacts_to_collect():
    """The receive side does exactly one thing: set the collect event."""
    source = ast.parse(Path(connection.__file__).read_text(encoding="utf-8"))
    loop = next(
        n
        for n in ast.walk(source)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "receive_loop"
    )
    calls = {call_name(n) for n in ast.walk(loop) if isinstance(n, ast.Call)}
    assert calls == {"parse_server_message", "logger.info", "collect.set"}
