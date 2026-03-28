from __future__ import annotations

import sys
import traceback

from application.tasks import execute_task
from core.registry import load_all


def main() -> int:
    if len(sys.argv) < 2:
        print("missing task_id", file=sys.stderr)
        return 2
    task_id = str(sys.argv[1] or "").strip()
    if not task_id:
        print("empty task_id", file=sys.stderr)
        return 2
    load_all()
    execute_task(task_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise
