import os

import uvicorn

from app.config import settings


if __name__ == "__main__":
    # Railway (and most cloud platforms) inject PORT; fall back to APP_PORT / 8000
    port = int(os.getenv("PORT", os.getenv("APP_PORT", "8000")))
    host = os.getenv("APP_HOST", "0.0.0.0")  # 0.0.0.0 required on Railway

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=os.getenv("APP_RELOAD", "false").lower() == "true",
        access_log=False,
        log_config=None,
    )
