"""Run from the project root: python tests/run_postgres_tests.py.

Creates a private temporary PostgreSQL cluster, runs the integration tests,
then stops that cluster and removes only its temporary directory.
Requires installed PostgreSQL server binaries and its matching pgvector.
"""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode


def run(command, **kwargs):
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=60, **kwargs
    )
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tests-dir", default="tests")
    parser.add_argument(
        "--test-file",
        default="test_postgres_indexing.py",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run every test file with the temporary PostgreSQL available.",
    )
    args = parser.parse_args()
    if not Path("app/db/models.py").is_file():
        parser.error("Run this command from the PAIagent project root.")
    test_dir = Path(args.tests_dir).resolve()
    if not args.all and not (test_dir / args.test_file).is_file():
        parser.error(f"{args.test_file} is missing from the tests directory.")
    pg_config = shutil.which("pg_config")
    if pg_config is None:
        parser.error("pg_config is missing; install PostgreSQL server tools first.")
    binaries = Path(run([pg_config, "--bindir"]))
    extension = Path(run([pg_config, "--sharedir"])) / "extension/vector.control"
    if not extension.is_file():
        parser.error("pgvector is not installed for this PostgreSQL version.")
    for name in ("initdb", "pg_ctl", "createdb", "psql"):
        if not (binaries / name).is_file():
            parser.error(f"Missing PostgreSQL server tool: {name}")

    directory = Path(tempfile.mkdtemp(prefix="paiagent-pg-", dir="/tmp"))
    data = directory / "data"
    socket = directory / "socket"
    socket.mkdir(mode=0o700)
    try:
        print(run([str(binaries / "initdb"), "--version"]), flush=True)
        run(
            [
                str(binaries / "initdb"),
                "-D",
                str(data),
                "-U",
                "postgres",
                "-A",
                "trust",
                "--no-locale",
                "--encoding=UTF8",
            ]
        )
        options = shlex.join(
            [
                "-c",
                "listen_addresses=",
                "-c",
                f"unix_socket_directories={socket}",
                "-p",
                "5432",
            ]
        )
        run(
            [
                str(binaries / "pg_ctl"),
                "-D",
                str(data),
                "-l",
                str(directory / "server.log"),
                "-o",
                options,
                "-w",
                "start",
            ]
        )
        connection = ["-h", str(socket), "-p", "5432", "-U", "postgres"]
        run([str(binaries / "createdb"), *connection, "paiagent_test"])
        run(
            [
                str(binaries / "psql"),
                "-X",
                *connection,
                "-d",
                "paiagent_test",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "CREATE EXTENSION vector WITH SCHEMA public",
            ]
        )
        version = run(
            [
                str(binaries / "psql"),
                "-X",
                *connection,
                "-d",
                "paiagent_test",
                "-Atc",
                "SELECT extversion FROM pg_extension WHERE extname='vector'",
            ]
        )
        print(f"Temporary PostgreSQL ready; pgvector {version}", flush=True)
        environment = os.environ.copy()
        environment["PAIAGENT_TEST_DATABASE_URL"] = (
            "postgresql+asyncpg://postgres@/paiagent_test?"
            + urlencode({"host": str(socket), "port": "5432"})
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(test_dir),
                "-p",
                "test_*.py" if args.all else args.test_file,
                "-v",
            ],
            env=environment,
            timeout=120,
        )
        return result.returncode
    finally:
        safe_to_remove = True
        if (data / "postmaster.pid").exists():
            try:
                run(
                    [
                        str(binaries / "pg_ctl"),
                        "-D",
                        str(data),
                        "-m",
                        "fast",
                        "-w",
                        "stop",
                    ]
                )
            except Exception:
                safe_to_remove = False
                print(
                    f"Could not stop temporary PostgreSQL; files retained at {directory}",
                    file=sys.stderr,
                )
                raise
        if safe_to_remove:
            shutil.rmtree(directory)
            print("Temporary PostgreSQL stopped and removed.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
