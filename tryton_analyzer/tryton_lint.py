#!/usr/bin/env python
import sys
from collections.abc import Sequence

from .pool import PoolManager
from .tools import print_diagnostics


def print_modules_diagnostics(module_names: Sequence[str]) -> None:
    manager = PoolManager()
    for module_name in module_names:
        print_diagnostics(
            module_name, manager.generate_module_diagnostics(module_name)
        )


def run() -> None:
    if (
        len(sys.argv) >= 2
        and sys.argv[1] in ("-h", "--help", "help")
        or len(sys.argv) == 1
    ):
        print(
            """Print diagnostics for a whole module

usage: tryton_lint [module1] [module2] ..."""
        )
    else:
        print_modules_diagnostics(sys.argv[1:])


if __name__ == "__main__":
    run()
