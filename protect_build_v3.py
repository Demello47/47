import argparse
import ast
import builtins
import os
import py_compile
import subprocess
import sys
from pathlib import Path


PROTECT_BUILD_VERSION = "3.0-import-safe"

BASE_PROTECTED_NAMES = {
    "main",
    "__name__",
    "__main__",
    "self",
    "cls",
}


class StripDocstrings(ast.NodeTransformer):
    def _strip_docstring(self, node):
        self.generic_visit(node)
        body = getattr(node, "body", None)

        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)

        return node

    def visit_Module(self, node):
        return self._strip_docstring(node)

    def visit_FunctionDef(self, node):
        return self._strip_docstring(node)

    def visit_AsyncFunctionDef(self, node):
        return self._strip_docstring(node)

    def visit_ClassDef(self, node):
        return self._strip_docstring(node)


class ProtectedNameCollector(ast.NodeVisitor):
    """
    Collect names that should not be renamed.

    Important:
    - imported module/object names are protected;
    - class names are protected;
    - function names are protected;
    - Python builtins are protected;
    - dunder names are protected separately by the obfuscator.

    This makes the obfuscation more conservative, but much less likely
    to break a working program.
    """

    def __init__(self):
        self.names = set(BASE_PROTECTED_NAMES)
        self.names.update(dir(builtins))
        self.has_star_import = False

    def visit_Import(self, node):
        for alias in node.names:
            # import os.path -> bound name is "os"
            # import numpy as np -> bound name is "np"
            bound = alias.asname or alias.name.split(".")[0]
            self.names.add(bound)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.name == "*":
                self.has_star_import = True
                continue

            bound = alias.asname or alias.name
            self.names.add(bound)

    def visit_FunctionDef(self, node):
        self.names.add(node.name)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self.names.add(node.name)
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.names.add(node.name)
        self.generic_visit(node)


class ObfuscateNames(ast.NodeTransformer):
    """
    Conservative AST identifier obfuscation.

    Renames ordinary variable/argument identifiers while preserving:
    - imports
    - functions
    - classes
    - Python builtins
    - self / cls
    - dunder names

    Attribute names such as sys.argv, obj.method, Path.exists are not renamed.

    WARNING:
    Any automatic identifier renaming can still affect code that uses
    variable names dynamically through strings, globals(), locals(), eval(),
    exec(), inspect, serialization, plugin systems, etc.
    """

    def __init__(self, protected_names):
        self.mapping = {}
        self.counter = 0
        self.protected = set(protected_names)

    def _new_name(self):
        self.counter += 1
        return f"_v{self.counter:x}"

    def _should_protect(self, name):
        return (
            name in self.protected
            or name.startswith("__")
            or not name.isidentifier()
        )

    def _mapped(self, name):
        if self._should_protect(name):
            return name

        if name not in self.mapping:
            self.mapping[name] = self._new_name()

        return self.mapping[name]

    def _rename_arguments(self, args):
        all_args = (
            list(args.posonlyargs)
            + list(args.args)
            + list(args.kwonlyargs)
        )

        for arg in all_args:
            if arg.arg not in {"self", "cls"}:
                arg.arg = self._mapped(arg.arg)

        if args.vararg and args.vararg.arg not in {"self", "cls"}:
            args.vararg.arg = self._mapped(args.vararg.arg)

        if args.kwarg and args.kwarg.arg not in {"self", "cls"}:
            args.kwarg.arg = self._mapped(args.kwarg.arg)

    def visit_FunctionDef(self, node):
        # Function name itself is intentionally preserved.
        self._rename_arguments(node.args)
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node):
        # Function name itself is intentionally preserved.
        self._rename_arguments(node.args)
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node):
        # Class name itself is intentionally preserved.
        self.generic_visit(node)
        return node

    def visit_Name(self, node):
        node.id = self._mapped(node.id)
        return node


def parse_source(input_file):
    source = input_file.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(input_file))


def clean_tree(tree):
    tree = StripDocstrings().visit(tree)
    ast.fix_missing_locations(tree)
    return tree


def clean_source(input_file, output_file):
    tree = parse_source(input_file)
    tree = clean_tree(tree)

    output_file.write_text(
        ast.unparse(tree) + "\n",
        encoding="utf-8",
    )


def obfuscate_source(input_file, output_file):
    tree = parse_source(input_file)
    tree = clean_tree(tree)

    collector = ProtectedNameCollector()
    collector.visit(tree)

    if collector.has_star_import:
        print(
            "WARNING: source contains 'from ... import *'. "
            "Automatic name obfuscation may be unsafe."
        )

    tree = ObfuscateNames(collector.names).visit(tree)
    ast.fix_missing_locations(tree)

    output_file.write_text(
        ast.unparse(tree) + "\n",
        encoding="utf-8",
    )

    # Extra safety check: imported bound names must still exist unchanged.
    generated_tree = ast.parse(
        output_file.read_text(encoding="utf-8"),
        filename=str(output_file),
    )
    generated_imports = ProtectedNameCollector()
    generated_imports.visit(generated_tree)

    original_import_names = set()
    original_tree = parse_source(input_file)
    for node in ast.walk(original_tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                original_import_names.add(
                    alias.asname or alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    original_import_names.add(alias.asname or alias.name)

    missing = sorted(
        name for name in original_import_names
        if name not in generated_imports.names
    )
    if missing:
        raise RuntimeError(
            "Import-safety check failed. Missing imported names: "
            + ", ".join(missing)
        )


def verify_python_file(file_path):
    try:
        py_compile.compile(
            str(file_path),
            doraise=True,
        )
        return True
    except py_compile.PyCompileError as exc:
        print(f"ERROR: {exc}")
        return False


def compile_to_pyc(source_file, output_file):
    py_compile.compile(
        str(source_file),
        cfile=str(output_file),
        doraise=True,
        optimize=2,
    )


def nuitka_installed():
    result = subprocess.run(
        [sys.executable, "-m", "nuitka", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def build_exe(source_file, output_dir, exe_name):
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--onefile",
        "--assume-yes-for-downloads",
        "--remove-output",
        f"--output-dir={output_dir}",
        f"--output-filename={exe_name}",
        str(source_file),
    ]

    print("\nRunning Nuitka:")
    print(" ".join(f'"{x}"' if " " in x else x for x in cmd))
    print()

    subprocess.run(cmd, check=True)


def smoke_test(file_path, test_args):
    """
    Optional runtime smoke test.

    Example:
        --test-args TEST_FOLDER

    The arguments are passed to the generated Python file exactly as supplied.
    A zero exit code means the smoke test passed.
    """

    cmd = [sys.executable, str(file_path)] + test_args

    print("\nRuntime smoke test:")
    print(" ".join(f'"{x}"' if " " in x else x for x in cmd))

    result = subprocess.run(cmd)

    if result.returncode != 0:
        print(
            f"\nWARNING: runtime smoke test returned "
            f"exit code {result.returncode}."
        )
        return False

    print("\nRuntime smoke test passed.")
    return True


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Clean, obfuscate, compile to PYC, or build a Windows EXE "
            "from a Python source file."
        )
    )

    parser.add_argument(
        "input",
        help="Input Python file, for example search_fails.py",
    )

    group = parser.add_mutually_exclusive_group()

    group.add_argument(
        "--clean-only",
        action="store_true",
        help="Remove comments/docstrings and save a cleaned .py file.",
    )

    group.add_argument(
        "--obfuscate-only",
        action="store_true",
        help="Remove comments/docstrings, rename safe identifiers, and save .py.",
    )

    group.add_argument(
        "--pyc-only",
        action="store_true",
        help="Obfuscate first, then compile to a standalone .pyc file.",
    )

    parser.add_argument(
        "--keep-intermediate",
        action="store_true",
        help="Keep intermediate obfuscated .py files.",
    )

    parser.add_argument(
        "--output-dir",
        default="protected_build",
        help="EXE output directory. Default: protected_build",
    )

    parser.add_argument(
        "--exe-name",
        default=None,
        help="Custom EXE filename, for example mytool.exe",
    )

    parser.add_argument(
        "--test-args",
        nargs=argparse.REMAINDER,
        default=None,
        help=(
            "Optionally run the generated .py before PYC/EXE creation. "
            "Everything after --test-args is passed to your program."
        ),
    )

    args = parser.parse_args()

    print(f"protect_build version: {PROTECT_BUILD_VERSION}")

    src = Path(args.input).resolve()

    if not src.is_file():
        print(f"ERROR: file not found: {src}")
        sys.exit(1)

    if src.suffix.lower() != ".py":
        print("ERROR: input file must end with .py")
        sys.exit(1)

    clean_file = src.with_name(src.stem + "_clean.py")
    obfuscated_file = src.with_name(src.stem + "_v3_obfuscated.py")
    pyc_file = src.with_name(src.stem + "_v3_obfuscated.pyc")

    output_dir = Path(args.output_dir).resolve()

    exe_name = args.exe_name or (src.stem + ".exe")
    if not exe_name.lower().endswith(".exe"):
        exe_name += ".exe"

    # ------------------------------------------------------------
    # CLEAN ONLY
    # ------------------------------------------------------------
    if args.clean_only:
        clean_source(src, clean_file)

        if not verify_python_file(clean_file):
            print("ERROR: cleaned file failed compilation test.")
            sys.exit(1)

        print("\nDone.")
        print(f"Cleaned source: {clean_file}")
        return

    # ------------------------------------------------------------
    # OBFUSCATE
    # ------------------------------------------------------------
    obfuscate_source(src, obfuscated_file)

    if not verify_python_file(obfuscated_file):
        print("ERROR: obfuscated file failed compilation test.")
        sys.exit(1)

    print(f"Obfuscated source passed py_compile: {obfuscated_file}")

    if args.test_args is not None:
        if not smoke_test(obfuscated_file, args.test_args):
            print(
                "\nBuild stopped because the optional runtime test failed."
            )
            sys.exit(3)

    # ------------------------------------------------------------
    # OBFUSCATE ONLY
    # ------------------------------------------------------------
    if args.obfuscate_only:
        print("\nDone.")
        print(f"Obfuscated source: {obfuscated_file}")
        print(
            "Imported module/object names, function names and class names "
            "were preserved."
        )
        return

    # ------------------------------------------------------------
    # PYC ONLY
    # ------------------------------------------------------------
    if args.pyc_only:
        try:
            compile_to_pyc(
                obfuscated_file,
                pyc_file,
            )
        except py_compile.PyCompileError as exc:
            print(f"ERROR: could not create PYC: {exc}")
            sys.exit(1)

        if not args.keep_intermediate:
            try:
                obfuscated_file.unlink()
            except OSError:
                pass

        print("\nDone.")
        print(f"PYC: {pyc_file}")
        print("\nRun it with:")
        print(f'  python "{pyc_file.name}" ARGUMENTS')
        print(
            "\nNOTE: .pyc is Python bytecode, not strong source-code "
            "protection, and it is tied to compatible Python versions."
        )
        return

    # ------------------------------------------------------------
    # DEFAULT: OBFUSCATE + NUITKA EXE
    # ------------------------------------------------------------
    if not nuitka_installed():
        print("\nNuitka is not installed.")
        print("Install it with:")
        print(
            f'  "{sys.executable}" -m pip install '
            "nuitka ordered-set zstandard"
        )
        print("\nThen run this command again.")
        sys.exit(2)

    try:
        build_exe(
            obfuscated_file,
            output_dir,
            exe_name,
        )
    except subprocess.CalledProcessError as exc:
        print(
            f"\nERROR: Nuitka build failed "
            f"with exit code {exc.returncode}"
        )
        print(f"Intermediate file kept: {obfuscated_file}")
        sys.exit(exc.returncode)

    if not args.keep_intermediate:
        try:
            obfuscated_file.unlink()
        except OSError:
            pass

    exe_path = output_dir / exe_name

    print("\nBuild completed.")
    print(f"EXE: {exe_path}")


if __name__ == "__main__":
    main()
