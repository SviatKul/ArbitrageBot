"""Read and write .env file preserving comments and structure."""

from __future__ import annotations

from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def read_env() -> dict[str, str]:
    """Return all KEY=VALUE pairs from .env (strips comments and blanks)."""
    result: dict[str, str] = {}
    if not ENV_PATH.is_file():
        return result
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, _, val = stripped.partition("=")
            result[key.strip()] = val.strip()
    return result


def write_env(updates: dict[str, str]) -> None:
    """
    Update keys in .env in-place. Existing lines are rewritten;
    new keys are appended. Comments and blank lines are preserved.
    """
    if not ENV_PATH.is_file():
        ENV_PATH.write_text("", encoding="utf-8")

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    written: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            new_lines.append(line)
            continue
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                written.add(key)
                continue
        new_lines.append(line)

    # Append keys not yet in file
    for key, val in updates.items():
        if key not in written:
            new_lines.append(f"{key}={val}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
