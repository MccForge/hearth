"""python -m hearth: load an optional .env from the working directory, then serve."""
import os


def _load_env(path: str = ".env") -> None:
    """KEY=value lines; existing environment wins. Lets a Bedrock key or SMTP password live outside the repo and the shell history."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_env()
from hearth.app import main  # noqa: E402

main()
