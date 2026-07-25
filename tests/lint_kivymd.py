#!/usr/bin/env python3
"""Static linter for ``app/main.py`` -- catches the failure classes listed in
CLAUDE.md before a 30-minute Buildozer run does.

Checks (all report ``file:line``):

  KVMD001  KivyMD 2.x-only widget name (MDButton, MDSnackbarText, ...).
  KVMD002  Widget known not to work in this build (TwoLineAvatarIconListItem).
  KVMD003  Widget/class named in KV that does not exist in the *installed*
           KivyMD 1.2.0 / Kivy 2.3.1 -- resolved for real via
           ``kivy.factory.Factory`` plus a sweep of ``kivymd.uix`` /
           ``kivy.uix``, not against a hardcoded list.
  KVMD004  ``from kivy*/kivymd* import X`` where X does not exist.  These
           imports are usually wrapped in ``try/except ImportError`` in the
           app, so a bad name fails *silently* on the phone.
  PYJN001  Python ``bytes`` (not ``bytearray``) handed to an OutputStream-ish
           ``write()``.  pyjnius cannot marshal bytes into a Java ``byte[]``.
  LAMB001  Lambda inside a loop capturing the loop variable without a
           default-arg binding (late-binding closure bug).

Warnings (reported, do not fail the build):

  KVMD101  KV sets a property the resolved class does not define.
  PY101    Unused import.

Usage::

    python3 tests/lint_kivymd.py [path ...]      # exit 1 on any failure
    python3 tests/lint_kivymd.py --quiet
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402  (must run before kivy is imported)


# ---------------------------------------------------------------------------
# Known-bad names
# ---------------------------------------------------------------------------

# Widgets introduced by the KivyMD 2.x rewrite.  None of these exist in 1.2.0.
# ``tests/test_app_smoke.py::test_kivymd2_denylist_is_accurate`` asserts that,
# so the list can never silently drift out of date.
KIVYMD_2X_ONLY = {
    # buttons
    "MDButton",
    "MDButtonText",
    "MDButtonIcon",
    "MDFabButton",
    "MDExtendedFabButton",
    "MDExtendedFabButtonText",
    # top app bar
    "MDTopAppBarTitle",
    "MDTopAppBarLeadingButtonContainer",
    "MDTopAppBarTrailingButtonContainer",
    "MDActionTopAppBarButton",
    # lists
    "MDListItem",
    "MDListItemHeadlineText",
    "MDListItemSupportingText",
    "MDListItemTertiaryText",
    "MDListItemLeadingIcon",
    "MDListItemLeadingAvatar",
    "MDListItemTrailingIcon",
    "MDListItemTrailingCheckbox",
    "MDListItemTrailingSupportingText",
    # snackbar
    "MDSnackbarText",
    "MDSnackbarSupportingText",
    "MDSnackbarButtonContainer",
    "MDSnackbarActionButtonText",
    # text field
    "MDTextFieldHintText",
    "MDTextFieldHelperText",
    "MDTextFieldLeadingIcon",
    "MDTextFieldTrailingIcon",
    "MDTextFieldMaxLengthText",
    # dialog
    "MDDialogHeadlineText",
    "MDDialogSupportingText",
    "MDDialogIcon",
    "MDDialogButtonContainer",
    "MDDialogContentContainer",
    # misc renames
    "MDDivider",  # 1.2.0 calls it MDSeparator
    "MDNavigationBar",
    "MDNavigationItem",
    "MDNavigationItemLabel",
    "MDNavigationItemIcon",
    "MDNavigationDrawerItemText",
    "MDSliderHandle",
    "MDSliderValueLabel",
    "MDTabsPrimary",
    "MDTabsSecondary",
    "MDTabsItem",
    "MDTabsItemText",
    "MDTabsItemIcon",
    "MDTabsItemSecondary",
    "MDTabsBadge",
    "MDCheckboxIcon",
    "MDLabelText",
}

# Exists in KivyMD 1.2.0 but is documented in CLAUDE.md as not working in this
# build ("use TwoLineAvatarListItem").
KNOWN_BROKEN = {
    "TwoLineAvatarIconListItem": "does not work in this build -- use TwoLineAvatarListItem",
    "OneLineAvatarIconListItem": "avatar+icon list items are unreliable in this build",
    "ThreeLineAvatarIconListItem": "avatar+icon list items are unreliable in this build",
}

# KV keywords / pseudo-names that are not widgets.
KV_NON_WIDGETS = {
    "Builder",
    "Factory",
    "Clock",
    "Window",
    "Animation",
    "Metrics",
    "None",
    "True",
    "False",
}

# Properties that never belong to the widget class itself.
KV_META_PROPS = {"id", "canvas", "canvas.before", "canvas.after"}


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    path: str
    line: int
    code: str
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        tag = "ERROR" if self.severity == "error" else "warn "
        return f"{self.path}:{self.line}: [{tag} {self.code}] {self.message}"


@dataclass
class LintResult:
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        (self.errors if finding.severity == "error" else self.warnings).append(finding)

    def extend(self, other: "LintResult") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)

    @property
    def ok(self) -> bool:
        return not self.errors

    def report(self, stream=sys.stdout, quiet: bool = False) -> None:
        for finding in sorted(self.warnings, key=lambda f: (f.path, f.line)):
            print(finding, file=stream)
        for finding in sorted(self.errors, key=lambda f: (f.path, f.line)):
            print(finding, file=stream)
        if not quiet:
            print(
                f"\nlint_kivymd: {len(self.errors)} error(s), "
                f"{len(self.warnings)} warning(s)",
                file=stream,
            )


# ---------------------------------------------------------------------------
# Name resolution against the *installed* KivyMD / Kivy
# ---------------------------------------------------------------------------

_resolver_ready = False
_module_symbols: dict[str, str] = {}


def _prepare_resolver() -> None:
    """Import Kivy/KivyMD and index every public widget class name.

    A Window has to exist first: several ``kivymd.uix`` modules instantiate
    widgets at import time, and ``Widget.__init__`` aborts the process when
    ``EventLoop`` has no window.
    """
    global _resolver_ready
    if _resolver_ready:
        return
    harness.install_window()

    import importlib
    import pkgutil

    import kivy.uix
    import kivymd.uix

    import kivymd  # noqa: F401  (registers KivyMD names with Factory)

    for package in (kivymd.uix, kivy.uix):
        for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            try:
                module = importlib.import_module(info.name)
            except Exception:
                continue
            for attr, obj in vars(module).items():
                if isinstance(obj, type) and attr not in _module_symbols:
                    _module_symbols[attr] = info.name

    for extra in ("kivy.graphics", "kivy.graphics.vertex_instructions", "kivymd.uix.behaviors"):
        try:
            module = importlib.import_module(extra)
        except Exception:
            continue
        for attr, obj in vars(module).items():
            if isinstance(obj, type) and attr not in _module_symbols:
                _module_symbols[attr] = extra

    _resolver_ready = True


def resolve_widget_name(name: str):
    """Return (ok, cls_or_None) for a widget/class name used in KV or Python."""
    _prepare_resolver()
    from kivy.factory import Factory

    try:
        return True, Factory.get(name)
    except Exception:
        pass
    if name in _module_symbols:
        import importlib

        try:
            return True, getattr(importlib.import_module(_module_symbols[name]), name)
        except Exception:
            return True, None
    return False, None


# ---------------------------------------------------------------------------
# KV extraction
# ---------------------------------------------------------------------------


@dataclass
class KvBlock:
    path: str
    text: str
    first_line: int  # file line number of text line index 0

    def line_of(self, index: int) -> int:
        return self.first_line + index


def extract_kv_blocks(path: Path, tree: ast.AST | None = None) -> list[KvBlock]:
    """Every KV-language string in a Python file, with file line numbers."""
    source = path.read_text()
    if path.suffix == ".kv":
        return [KvBlock(str(path), source, 1)]

    tree = tree or ast.parse(source)
    constants: dict[str, ast.Constant] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value

    picked: dict[int, ast.Constant] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if fname in ("load_string", "load_file"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        picked[id(arg)] = arg
                    elif isinstance(arg, ast.Name) and arg.id in constants:
                        picked[id(constants[arg.id])] = constants[arg.id]

    if not picked:
        for const in constants.values():
            if harness._looks_like_kv(const.value):
                picked[id(const)] = const

    blocks = []
    for const in picked.values():
        # ast.Constant.lineno is the line holding the opening quote; the text's
        # own line 0 is the remainder of that same line.
        blocks.append(KvBlock(str(path), const.value, const.lineno))
    return blocks


_KV_WIDGET_RE = re.compile(r"^(\s*)([A-Za-z_][\w.]*)\s*:\s*$")
_KV_RULE_RE = re.compile(r"^\s*<-?([A-Za-z_]\w*)(?:@([A-Za-z_][\w+,]*))?>\s*:\s*$")
_KV_PROP_RE = re.compile(r"^(\s*)([a-z_][\w.]*)\s*:\s*(\S.*)$")
_KV_APP_METHOD_RE = re.compile(r"\bapp\.([A-Za-z_]\w*)\s*\(([^()]*)\)")


def parse_kv_widgets(block: KvBlock) -> list[tuple[int, str, int]]:
    """-> [(file_line, WidgetName, indent)] for every widget instantiation."""
    out = []
    for idx, raw in enumerate(block.text.split("\n")):
        line = raw.split("#", 1)[0].rstrip() if "#" in raw else raw.rstrip()
        if not line.strip():
            continue
        if _KV_RULE_RE.match(line):
            continue
        match = _KV_WIDGET_RE.match(line)
        if not match:
            continue
        indent, name = match.group(1), match.group(2)
        if "." in name or not name[0].isupper():
            continue  # canvas.before, property names, ...
        out.append((block.line_of(idx), name, len(indent)))
    return out


def parse_kv_dynamic_classes(block: KvBlock) -> dict[str, str | None]:
    """-> {DynamicClassName: BaseName or None} for ``<Foo@Bar>:`` rules."""
    out: dict[str, str | None] = {}
    for raw in block.text.split("\n"):
        match = _KV_RULE_RE.match(raw.rstrip())
        if match:
            out[match.group(1)] = match.group(2)
    return out


def parse_kv_app_calls(block: KvBlock) -> list[tuple[int, str, int]]:
    """-> [(file_line, method_name, positional_arg_count)] for ``app.foo(...)``."""
    out = []
    for idx, raw in enumerate(block.text.split("\n")):
        for match in _KV_APP_METHOD_RE.finditer(raw):
            args = match.group(2).strip()
            count = 0 if not args else len([a for a in _split_args(args) if a.strip()])
            out.append((block.line_of(idx), match.group(1), count))
    return out


def _split_args(text: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current)
    return parts


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_syntax(path: Path) -> LintResult:
    result = LintResult()
    try:
        ast.parse(path.read_text(), filename=str(path))
    except SyntaxError as exc:
        result.add(
            Finding(str(path), exc.lineno or 1, "SYN001", f"syntax error: {exc.msg}")
        )
    return result


def check_denylisted_names(path: Path, blocks: list[KvBlock]) -> LintResult:
    """KVMD001 / KVMD002 over the whole file (Python *and* KV)."""
    result = LintResult()
    lines = path.read_text().split("\n")
    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        for name in KIVYMD_2X_ONLY:
            if re.search(rf"\b{name}\b", raw):
                result.add(
                    Finding(
                        str(path),
                        lineno,
                        "KVMD001",
                        f"'{name}' is a KivyMD 2.x-only widget; this project pins KivyMD 1.2.0",
                    )
                )
        for name, why in KNOWN_BROKEN.items():
            if re.search(rf"\b{name}\b", raw):
                result.add(Finding(str(path), lineno, "KVMD002", f"'{name}' {why}"))
    return result


def check_kv_names(path: Path, blocks: list[KvBlock]) -> LintResult:
    """KVMD003 (+ KVMD101 warning): every KV name resolves in the installed libs."""
    result = LintResult()
    for block in blocks:
        dynamic = parse_kv_dynamic_classes(block)
        local_names = set(dynamic)
        for base in dynamic.values():
            if base:
                for part in re.split(r"[+,]", base):
                    part = part.strip()
                    if part and part not in local_names:
                        ok, _cls = resolve_widget_name(part)
                        if not ok:
                            result.add(
                                Finding(
                                    block.path,
                                    block.first_line,
                                    "KVMD003",
                                    f"KV dynamic class inherits from unknown base '{part}'",
                                )
                            )

        widgets = parse_kv_widgets(block)
        for file_line, name, _indent in widgets:
            if name in KV_NON_WIDGETS or name in local_names:
                continue
            ok, _cls = resolve_widget_name(name)
            if not ok:
                result.add(
                    Finding(
                        block.path,
                        file_line,
                        "KVMD003",
                        f"'{name}' does not exist in the installed "
                        f"KivyMD {_kivymd_version()} / Kivy {_kivy_version()}",
                    )
                )

        result.extend(_check_kv_properties(block, widgets, local_names))
    return result


def _check_kv_properties(block: KvBlock, widgets, local_names) -> LintResult:
    """KVMD101: property set in KV that the resolved class does not define."""
    result = LintResult()
    lines = block.text.split("\n")
    # Map indent -> current widget name, walking the KV tree top-down.
    stack: list[tuple[int, str]] = []
    widget_at_line = {line: (name, indent) for line, name, indent in widgets}
    in_canvas_indent = None

    for idx, raw in enumerate(lines):
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        file_line = block.line_of(idx)
        indent = len(line) - len(line.lstrip())

        if in_canvas_indent is not None and indent > in_canvas_indent:
            continue
        in_canvas_indent = None

        if file_line in widget_at_line:
            name, w_indent = widget_at_line[file_line]
            while stack and stack[-1][0] >= w_indent:
                stack.pop()
            stack.append((w_indent, name))
            continue

        match = _KV_PROP_RE.match(line)
        if not match:
            # bare `canvas:` / `canvas.before:` blocks
            bare = _KV_WIDGET_RE.match(line)
            if bare and bare.group(2).split(".")[0] == "canvas":
                in_canvas_indent = indent
            continue

        prop = match.group(2)
        if prop in KV_META_PROPS or prop.startswith("on_") or "." in prop:
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            continue
        owner = stack[-1][1]
        if owner in local_names:
            continue
        ok, cls = resolve_widget_name(owner)
        if not ok or cls is None:
            continue
        if not hasattr(cls, prop):
            result.add(
                Finding(
                    block.path,
                    file_line,
                    "KVMD101",
                    f"'{owner}' has no property '{prop}' in the installed KivyMD/Kivy",
                    severity="warning",
                )
            )
    return result


def check_python_imports(path: Path, tree: ast.AST) -> LintResult:
    """KVMD004: ``from kivy*/kivymd* import X`` where X is not there."""
    import importlib

    result = LintResult()
    _prepare_resolver()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            if module_name.split(".")[0] not in ("kivy", "kivymd"):
                continue
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:
                result.add(
                    Finding(
                        str(path),
                        node.lineno,
                        "KVMD004",
                        f"cannot import module '{module_name}': {exc}",
                    )
                )
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                if not hasattr(module, alias.name):
                    result.add(
                        Finding(
                            str(path),
                            node.lineno,
                            "KVMD004",
                            f"'{module_name}' has no attribute '{alias.name}' "
                            f"in the installed KivyMD {_kivymd_version()}",
                        )
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ("kivy", "kivymd"):
                    continue
                try:
                    importlib.import_module(alias.name)
                except Exception as exc:
                    result.add(
                        Finding(
                            str(path),
                            node.lineno,
                            "KVMD004",
                            f"cannot import '{alias.name}': {exc}",
                        )
                    )

    # Factory.MDFoo / Factory.get("MDFoo") lookups in Python resolve lazily at
    # runtime, so a bad name there is invisible until the widget is built.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _dotted(node.value) == "Factory":
            name = node.attr
            if name in ("get", "register", "unregister"):
                continue
            if not resolve_widget_name(name)[0]:
                result.add(
                    Finding(
                        str(path),
                        node.lineno,
                        "KVMD003",
                        f"Factory.{name} does not exist in the installed "
                        f"KivyMD {_kivymd_version()} / Kivy {_kivy_version()}",
                    )
                )
        elif isinstance(node, ast.Call) and _dotted(node.func) == "Factory.get":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if not resolve_widget_name(arg.value)[0]:
                        result.add(
                            Finding(
                                str(path),
                                node.lineno,
                                "KVMD003",
                                f"Factory.get('{arg.value}') does not exist in the "
                                f"installed KivyMD {_kivymd_version()}",
                            )
                        )
    return result


# -- PYJN001: bytes into a Java byte[] -------------------------------------

_STREAM_NAME_RE = re.compile(
    r"(?:^|_)(?:out|os|fos|bos|stream|outstream|outputstream)$|(?:out|output|stream)$",
    re.IGNORECASE,
)
_STREAM_FACTORY_CALLS = {
    "openOutputStream",
    "getOutputStream",
    "openFileOutput",
    "openAssetFileDescriptor",
}


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _is_bytes_expr(node: ast.AST, bytes_names: set[str]) -> str | None:
    """Return a human description if ``node`` evaluates to ``bytes``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, bytes):
        return "bytes literal"
    if isinstance(node, ast.Call):
        func = _dotted(node.func)
        if func == "bytes":
            return "bytes(...)"
        if func.endswith(".encode") or func == "encode":
            return "str.encode() (returns bytes)"
        if func in ("open", "io.open"):
            return None
    if isinstance(node, ast.Name) and node.id in bytes_names:
        return f"'{node.id}' holds bytes"
    if isinstance(node, ast.BinOp):
        for side in (node.left, node.right):
            found = _is_bytes_expr(side, bytes_names)
            if found:
                return found
    return None


def check_bytes_to_stream(path: Path, tree: ast.AST) -> LintResult:
    result = LintResult()
    bytes_names: set[str] = set()
    stream_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:
                continue
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if _is_bytes_expr(value, bytes_names):
                    bytes_names.add(target.id)
                if isinstance(value, ast.Call):
                    func = _dotted(value.func)
                    tail = func.rsplit(".", 1)[-1]
                    if "OutputStream" in func or tail in _STREAM_FACTORY_CALLS:
                        stream_names.add(target.id)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("write", "writeBytes"):
            continue
        recv = node.func.value
        recv_name = _dotted(recv)
        tail = recv_name.rsplit(".", 1)[-1]
        looks_like_stream = (
            tail in stream_names
            or "OutputStream" in recv_name
            or bool(_STREAM_NAME_RE.search(tail))
        )
        if not looks_like_stream or not node.args:
            continue
        why = _is_bytes_expr(node.args[0], bytes_names)
        if why:
            result.add(
                Finding(
                    str(path),
                    node.lineno,
                    "PYJN001",
                    f"{recv_name}.{node.func.attr}() is given {why}; pyjnius cannot "
                    f"convert Python bytes to Java byte[] -- wrap it in bytearray(...)",
                )
            )
    return result


# -- LAMB001: late-binding closures in loops --------------------------------


def _assigned_names(node: ast.AST) -> set[str]:
    names: set[str] = set()

    def collect(target):
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                collect(elt)
        elif isinstance(target, ast.Starred):
            collect(target.value)

    for child in ast.walk(node):
        if isinstance(child, (ast.For, ast.AsyncFor)):
            collect(child.target)
        elif isinstance(child, ast.Assign):
            for target in child.targets:
                collect(target)
        elif isinstance(child, (ast.AugAssign, ast.AnnAssign)):
            collect(child.target)
        elif isinstance(child, ast.comprehension):
            collect(child.target)
        elif isinstance(child, ast.withitem) and child.optional_vars is not None:
            collect(child.optional_vars)
    return names


def _lambda_bound_names(node: ast.Lambda) -> set[str]:
    args = node.args
    bound = {a.arg for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)}
    if args.vararg:
        bound.add(args.vararg.arg)
    if args.kwarg:
        bound.add(args.kwarg.arg)
    return bound


def _lambda_free_names(node: ast.Lambda) -> set[str]:
    bound = _lambda_bound_names(node)
    free = set()
    for child in ast.walk(node.body):
        if isinstance(child, ast.Lambda):
            continue
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            if child.id not in bound:
                free.add(child.id)
    return free


def check_lambda_late_binding(path: Path, tree: ast.AST) -> LintResult:
    result = LintResult()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            continue
        loop_vars = _assigned_names(node)
        if isinstance(node, (ast.For, ast.AsyncFor)):
            # the target itself is in _assigned_names already
            pass
        for child in ast.walk(node):
            if not isinstance(child, ast.Lambda):
                continue
            captured = sorted(_lambda_free_names(child) & loop_vars)
            if captured:
                names = ", ".join(captured)
                result.add(
                    Finding(
                        str(path),
                        child.lineno,
                        "LAMB001",
                        f"lambda in a loop captures loop variable(s) {names} by reference; "
                        f"bind by value with a default arg, e.g. "
                        f"lambda *a, {captured[0]}={captured[0]}: ...",
                    )
                )
    return result


# -- PY101: unused imports (warning, CLAUDE.md audit item 10) ---------------


def check_unused_imports(path: Path, tree: ast.AST) -> LintResult:
    result = LintResult()
    source = path.read_text()
    imported: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name.split(".")[0]
                imported.setdefault(bound, node.lineno)

    used = {
        child.id
        for child in ast.walk(tree)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    }
    used |= {
        child.attr for child in ast.walk(tree) if isinstance(child, ast.Attribute)
    }
    for child in ast.walk(tree):
        if isinstance(child, ast.Attribute):
            used.add(_dotted(child).split(".")[0])

    for name, lineno in imported.items():
        if name in used:
            continue
        # KV strings reference names too (e.g. dp() used only inside KV).
        if re.search(rf"\b{re.escape(name)}\b", source.split("\n", 1)[-1]) and (
            source.count(name) > 1
        ):
            continue
        result.add(
            Finding(str(path), lineno, "PY101", f"'{name}' imported but never used", "warning")
        )
    return result


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _kivymd_version() -> str:
    try:
        import kivymd

        return kivymd.__version__
    except Exception:  # pragma: no cover
        return "?"


def _kivy_version() -> str:
    try:
        import kivy

        return kivy.__version__
    except Exception:  # pragma: no cover
        return "?"


def lint_file(path: str | Path) -> LintResult:
    path = Path(path)
    result = LintResult()

    syntax = check_syntax(path)
    result.extend(syntax)
    if syntax.errors:
        return result  # nothing else can run on unparseable source

    if path.suffix == ".kv":
        blocks = extract_kv_blocks(path)
        result.extend(check_denylisted_names(path, blocks))
        result.extend(check_kv_names(path, blocks))
        return result

    tree = ast.parse(path.read_text(), filename=str(path))
    blocks = extract_kv_blocks(path, tree)

    result.extend(check_denylisted_names(path, blocks))
    result.extend(check_kv_names(path, blocks))
    result.extend(check_python_imports(path, tree))
    result.extend(check_bytes_to_stream(path, tree))
    result.extend(check_lambda_late_binding(path, tree))
    result.extend(check_unused_imports(path, tree))
    return result


def default_targets() -> list[Path]:
    targets = [harness.APP_MAIN]
    targets += sorted((harness.REPO_ROOT / "app").glob("*.kv"))
    return [p for p in targets if p.exists()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("paths", nargs="*", help="files to lint (default: app/main.py + app/*.kv)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    targets = [Path(p) for p in args.paths] or default_targets()
    if not targets:
        print("lint_kivymd: nothing to lint", file=sys.stderr)
        return 1

    total = LintResult()
    for target in targets:
        if not target.exists():
            total.add(Finding(str(target), 1, "IO001", "file not found"))
            continue
        total.extend(lint_file(target))

    if not args.quiet:
        print(
            f"lint_kivymd: KivyMD {_kivymd_version()}, Kivy {_kivy_version()}, "
            f"window={harness.WINDOW_KIND or 'not-required'}"
        )
        print("checked: " + ", ".join(str(t) for t in targets))
    total.report(quiet=args.quiet)
    return 0 if total.ok else 1


if __name__ == "__main__":
    sys.exit(main())
