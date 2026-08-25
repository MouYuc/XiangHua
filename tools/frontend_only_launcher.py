import os

import uvicorn


uvicorn.run(
    "backend.app:app",
    host=os.environ.get("XIANGHUA_SERVER_NAME", "127.0.0.1"),
    port=int(os.environ.get("XIANGHUA_SERVER_PORT", "7860")),
)
