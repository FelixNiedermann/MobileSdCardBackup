from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
try:
    from backend.fs_api import router
except ModuleNotFoundError:
    from fs_api import router

app = FastAPI()
app.include_router(router)

BASE_DIR = Path(__file__).resolve().parent.parent

@app.get("/")
def ui():
    return FileResponse(BASE_DIR / "ui" / "index.html")
