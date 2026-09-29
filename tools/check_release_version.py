"""確認發布標籤與程式內嵌版本完全一致。"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


APP_SOURCE = Path(__file__).resolve().parents[1] / "app.py"


def app_version(source: Path = APP_SOURCE) -> str:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    values = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "APP_VERSION"
    ]
    if len(values) != 1 or not isinstance(values[0], ast.Constant) or not isinstance(values[0].value, str):
        raise ValueError("app.py 必須恰有一個字串常值 APP_VERSION。")
    return values[0].value


def verify_release_tag(tag: str, source: Path = APP_SOURCE) -> None:
    expected = f"v{app_version(source)}"
    if tag != expected:
        raise ValueError(f"發布標籤 {tag!r} 與程式版本不符，預期為 {expected!r}。")


if __name__ == "__main__":
    try:
        verify_release_tag(sys.argv[1] if len(sys.argv) == 2 else "")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print("發布標籤與程式版本一致。")
