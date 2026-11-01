"""Launch the Vue/FastAPI XiangHua WebUI."""

import os
import sys
from pathlib import Path

import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> None:
    uvicorn.run(
        "backend.app:app",
        host=os.environ.get("XIANGHUA_SERVER_NAME", "127.0.0.1"),
        port=int(os.environ.get("XIANGHUA_SERVER_PORT", "7860")),
        reload=False,
    )


if __name__ == "__main__":
    main()
