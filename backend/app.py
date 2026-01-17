from fastapi import FastAPI
from fastapi.responses import FileResponse
try:
    from backend.fs_api import router
except ModuleNotFoundError:
    from fs_api import router

app = FastAPI()
app.include_router(router)

@app.get("/")
def ui():
    return FileResponse("ui/index.html")
