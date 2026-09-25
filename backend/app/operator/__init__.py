"""
Operator commands that ship inside the deployed image.

The Railway image is built from `backend/` alone, so the repository's
`scripts/` directory does not exist in the container. Anything an operator
needs to run against production therefore has to live under `app/`, where it
is invocable as:

    python -m app.operator.<command> --help

These modules are imported only when one of them is run. Nothing in the
serving path imports this package, so its presence cannot change production
behaviour — `tests/test_operator_commands.py` asserts that.

The repository's `scripts/*.py` are thin wrappers around exactly these
modules, so a command behaves identically whether it is run from a checkout
or from inside the container.
"""
