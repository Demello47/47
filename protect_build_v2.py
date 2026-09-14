import argparse
import ast
import subprocess
import sys
from pathlib import Path


class StripDocstrings(ast.NodeTransformer):
    def _strip(self, node):
        self.generic_visit(node)
        if (
            getattr(node, "body", None)
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body.pop(0)
        return node

    def visit_Module(self, node):
        return self._strip(node)

    def visit_FunctionDef(self, node):
        return self._strip(node)

    def visit_AsyncFunctionDef(self, node):
        return self._strip(node)

    def visit_ClassDef(self, node):
        return self._strip(node)



class ObfuscateNames(ast.NodeTransformer):
    def __init__(self):
        self.mapping = {}
        self.counter = 0
        self.protected = {
            "main", "__name__", "__main__", "self", "cls",
        }

    def _new_name(self):
        self.counter += 1
        return f"_v{self.counter:x}"

    def _mapped(self, name):
        if (
            name in self.protected
            or name.startswith("__")
            or not name.isidentifier()
        ):
            return name
        if name not in self.mapping:
            self.mapping[name] = self._new_name()
        return self.mapping[name]

    def visit_FunctionDef(self, node):
        if node.name != "main" and not node.name.startswith("__"):
            node.name = self._mapped(node.name)
        self._rename_args(node.args)
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node):
        if node.name != "main" and not node.name.startswith("__"):
            node.name = self._mapped(node.name)
        self._rename_args(node.args)
        self.generic_visit(node)
        return node

    def _rename_args(self, args):
        for arg in (
            list(args.posonlyargs)
            + list(args.args)
            + list(args.kwonlyargs)
        ):
            if arg.arg not in {"self", "cls"}:
                arg.arg = self._mapped(arg.arg)
        if args.vararg and args.vararg.arg not in {"self", "cls"}:
            args.vararg.arg = self._mapped(args.vararg.arg)
        if args.kwarg and args.kwarg.arg not in {"self", "cls"}:
            args.kwarg.arg = self._mapped(args.kwarg.arg)

    def visit_Name(self, node):
        # Do not rename Python builtins.
        import builtins
        if node.id not in dir(builtins):
            node.id = self._mapped(node.id)
        return node

def clean_source(input_file: Path, output_file: Path) -> None:
    source = input_file.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(input_file))
    tree = StripDocstrings().visit(tree)
    ast.fix_missing_locations(tree)
    output_file.write_text(ast.unparse(tree) + "\n", encoding="utf-8")



def obfuscate_source(input_file: Path, output_file: Path) -> None:
    source = input_file.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(input_file))

    # First remove docstrings/comments via AST round-trip.
    tree = StripDocstrings().visit(tree)
    ast.fix_missing_locations(tree)

    # Then rename ordinary functions/variables.
    tree = ObfuscateNames().visit(tree)
    ast.fix_missing_locations(tree)

    output_file.write_text(ast.unparse(tree) + "\n", encoding="utf-8")

def nuitka_installed() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "nuitka", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="Input Python file, for example search_fails.py")
    parser.add_argument("--clean-only", action="store_true")
    parser.add_argument("--obfuscate-only", action="store_true", help="Create an obfuscated .py file only; do not build EXE")
    parser.add_argument("--keep-clean", action="store_true")
    parser.add_argument("--output-dir", default="protected_build")
    args = parser.parse_args()

    src = Path(args.input).resolve()
    if not src.is_file() or src.suffix.lower() != ".py":
        print(f"ERROR: Python file not found: {src}")
        sys.exit(1)

    clean = src.with_name(src.stem + "_clean.py")
    obfuscated = src.with_name(src.stem + "_obfuscated.py")
    outdir = Path(args.output_dir).resolve()
    exe_name = src.stem + ".exe"

    if args.obfuscate_only:
        obfuscate_source(src, obfuscated)
        print(f"Obfuscated source created: {obfuscated}")
        check = subprocess.run([sys.executable, "-m", "py_compile", str(obfuscated)])
        if check.returncode != 0:
            print("ERROR: obfuscated source does not compile.")
            sys.exit(1)
        print("Obfuscated source passed py_compile.")
        print("IMPORTANT: test its runtime behavior before distribution.")
        return

    clean_source(src, clean)
    print(f"Cleaned source created: {clean}")

    check = subprocess.run([sys.executable, "-m", "py_compile", str(clean)])
    if check.returncode != 0:
        print("ERROR: cleaned source does not compile.")
        sys.exit(1)

    print("Cleaned source passed py_compile.")

    if args.clean_only:
        return

    if not nuitka_installed():
        print("\nNuitka is not installed.")
        print("Install it with:")
        print(f'  "{sys.executable}" -m pip install nuitka ordered-set zstandard')
        print("\nThen run this script again.")
        sys.exit(2)

    outdir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--onefile",
        "--assume-yes-for-downloads",
        "--remove-output",
        f"--output-dir={outdir}",
        f"--output-filename={exe_name}",
        str(clean),
    ]

    print("\nBuilding EXE...")
    subprocess.run(cmd, check=True)

    if not args.keep_clean:
        try:
            clean.unlink()
        except OSError:
            pass

    print(f"\nDone: {outdir / exe_name}")
    print("Keep keywords.txt and exclude.txt next to the EXE if your program uses them.")


if __name__ == "__main__":
    main()
