import pathlib
import shlex
import subprocess

ROOT_DIR = pathlib.Path.cwd().resolve()


def within_root(path: pathlib.Path) -> bool:
    """Return True if *path* is within the starting directory."""
    try:
        path.resolve(strict=False).relative_to(ROOT_DIR)
        return True
    except ValueError:
        return False


def token_is_path(token: str) -> bool:
    if token.startswith("-"):
        return False
    return token.startswith((".", "/", "~")) or "/" in token


def run_cmd(command: str) -> str:
    tokens = shlex.split(command)
    for t in tokens:
        if token_is_path(t):
            p = pathlib.Path(t).expanduser()
            if not within_root(p):
                return "Blocked: path outside working directory."
    try:
        res = subprocess.run(
            tokens,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return (res.stdout or "") + (res.stderr or "")
    except Exception as exc:
        return f"Command error: {exc}"
