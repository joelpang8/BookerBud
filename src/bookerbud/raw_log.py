"""Write every raw API response to disk so any run can be replayed/debugged."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SECRET_KEYS = {"apikey", "api_key", "x-api-key", "authorization"}


def redact(params: dict[str, Any]) -> dict[str, Any]:
    return {k: ("***" if k.lower() in _SECRET_KEYS else v) for k, v in params.items()}


class RawLogger:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)

    def write(
        self,
        provider: str,
        label: str,
        *,
        url: str,
        params: dict[str, Any],
        status: int,
        body: Any,
        now: datetime | None = None,
    ) -> Path:
        now = now or datetime.now(UTC)
        day_dir = self.base_dir / now.strftime("%Y-%m-%d") / provider
        day_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
        path = day_dir / f"{now.strftime('%H%M%S_%f')}_{safe}.json"
        record = {
            "provider": provider,
            "fetched_at": now.isoformat(),
            "url": url,
            "params": redact(params),
            "status": status,
            "body": body,
        }
        path.write_text(json.dumps(record, indent=1, ensure_ascii=False))
        return path


def read_raw(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())
