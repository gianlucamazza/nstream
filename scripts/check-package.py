"""Build and exercise the installed wheel in an isolated, disposable environment."""

import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="nstream-package-") as work:
        root = Path(work)
        subprocess.run(["uv", "build", "--out-dir", str(root / "dist")], cwd=ROOT, check=True)
        (wheel,) = (root / "dist").glob("*.whl")
        (source,) = (root / "dist").glob("*.tar.gz")
        with zipfile.ZipFile(wheel) as archive:
            assert "nstream/nstream.lua" in archive.namelist()
            assert "nstream/_bench.py" not in archive.namelist()
            assert "nstream/_subalign_remote.py" not in archive.namelist()
        with tarfile.open(source) as archive:
            assert any(name.endswith("src/nstream/nstream.lua") for name in archive.getnames())
        environment = root / "env"
        subprocess.run(["uv", "venv", "--python", sys.executable, str(environment)], check=True)
        python = environment / "bin" / "python"
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel)], check=True
        )
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
            directory = root / key
            directory.mkdir()
            env[key] = str(directory)
        for command in (
            [str(python), "-I", "-m", "nstream", "--version"],
            [str(environment / "bin" / "nstream"), "--version"],
        ):
            subprocess.run(command, cwd=root, env=env, check=True)
        subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "from importlib.resources import files; "
                "assert files('nstream').joinpath('nstream.lua').read_text()",
            ],
            cwd=root,
            env=env,
            check=True,
        )


if __name__ == "__main__":
    main()
