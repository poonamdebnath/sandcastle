from __future__ import annotations

from pathlib import Path


def render_template(template_path: Path, values: dict[str, object]) -> str:
    content = template_path.read_text(encoding="utf-8")
    rendered = content
    for key, value in values.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    return rendered
