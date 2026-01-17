from pathlib import Path
import threading, time
from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse

try:
    from backend.fs_api import router, auto_backup_check
except ModuleNotFoundError:
    from fs_api import router, auto_backup_check

app = FastAPI()
app.include_router(router)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["mobile-backup.local", "127.0.0.1", "localhost"],
)


def _auto_backup_loop():
    while True:
        try:
            auto_backup_check()
        except Exception:
            pass
        time.sleep(10)


@app.on_event("startup")
def start_auto_backup_thread():
    t = threading.Thread(target=_auto_backup_loop, daemon=True)
    t.start()

BASE_DIR = Path(__file__).resolve().parent.parent


@app.get("/")
def ui():
    return FileResponse(BASE_DIR / "ui" / "index.html")
