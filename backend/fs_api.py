from __future__ import annotations
import os, stat, subprocess, time, re, shutil, json, threading, mimetypes
from pathlib import Path
from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api", tags=["backup"])

MEDIA_ROOT = Path("/media/admin")
SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings.json"
DEFAULT_SETTINGS = {"auto_backup": False, "last_auto": None, "auto_latched": None}

JOB = {"running": False, "log": [], "progress": 0, "result": None, "finished_at": None}


def is_mounted(p: Path) -> bool:
    return p.exists() and p.is_dir() and os.path.ismount(p)


def safe_join(root: Path, rel: str) -> Path:
    rel = (rel or "").strip().lstrip("/")
    p = (root / rel).resolve()
    if not str(p).startswith(str(root.resolve())):
        raise HTTPException(400, "Invalid path")
    return p


def list_media_drives() -> dict[str, Path]:
    drives = {}
    if not MEDIA_ROOT.exists():
        return drives
    for p in MEDIA_ROOT.iterdir():
        if p.is_dir():
            drives[p.name] = p
    return drives


def available_drives():
    return {k: v for k, v in list_media_drives().items() if is_mounted(v)}


@router.get("/drives")
def drives():
    drives = []
    for k, v in list_media_drives().items():
        mounted = is_mounted(v)
        if mounted:
            st = v.stat()
            usage = shutil.disk_usage(v)
            space = {"free": usage.free, "total": usage.total}
            mtime = int(st.st_mtime)
        else:
            space = None
            mtime = None
        drives.append(
            {
                "id": k,
                "mounted": mounted,
                "path": str(v),
                "mtime": mtime,
                "space": space,
            }
        )
    return {"drives": drives}


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return DEFAULT_SETTINGS.copy()
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    settings = DEFAULT_SETTINGS.copy()
    if isinstance(data, dict):
        settings.update(data)
    return settings


def save_settings(settings: dict) -> None:
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    tmp.replace(SETTINGS_PATH)


@router.get("/settings")
def get_settings():
    return load_settings()


@router.post("/settings")
def set_settings(cfg: dict):
    settings = load_settings()
    if "auto_backup" in cfg:
        settings["auto_backup"] = bool(cfg["auto_backup"])
    save_settings(settings)
    return settings


@router.get("/list")
def list_dir(drive: str, path: str = ""):
    roots = available_drives()
    if drive not in roots:
        raise HTTPException(404)

    root = roots[drive]
    target = safe_join(root, path)
    if not target.exists() or not target.is_dir():
        raise HTTPException(404)

    entries = []
    with os.scandir(target) as it:
        for e in it:
            st = e.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                t = "dir"
            elif stat.S_ISREG(st.st_mode):
                t = "file"
            else:
                continue
            entries.append(
                {
                    "name": e.name,
                    "type": t,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                }
            )

    return {
        "drive": drive,
        "path": path,
        "entries": sorted(entries, key=lambda x: (x["type"], x["name"].lower())),
    }


def _calc_size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    for root, _, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


@router.post("/size")
def get_size(cfg: dict):
    roots = available_drives()
    drive = cfg.get("drive")
    if drive not in roots:
        raise HTTPException(404)
    paths = cfg.get("paths")
    if not isinstance(paths, list) or not paths:
        paths = [cfg.get("path", "")]
    total = 0
    for rel in paths:
        target = safe_join(roots[drive], rel or "")
        total += _calc_size(target)
    return {"bytes": total}


@router.get("/file")
def get_file(drive: str, path: str):
    roots = available_drives()
    if drive not in roots:
        raise HTTPException(404)
    target = safe_join(roots[drive], path)
    if not target.exists() or not target.is_file():
        raise HTTPException(404)
    media_type, _ = mimetypes.guess_type(str(target))
    return FileResponse(target, media_type=media_type)


def run_backup(cfg: dict):
    JOB.update(
        {"running": True, "log": [], "progress": 0, "result": None, "finished_at": None}
    )

    try:
        roots = available_drives()
        src_root = roots[cfg["src"]["drive"]]
        dst_root = roots[cfg["dst"]["drive"]]

        src_path = safe_join(src_root, cfg["src"]["path"])
        dst_base = safe_join(dst_root, cfg["dst"]["path"])

        overwrite = bool(cfg.get("overwrite"))
        verify = bool(cfg.get("verify"))

        folder_name = Path(src_path).name or "backup"
        name = (cfg.get("backup_name") or folder_name).replace(" ", "_")
        timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")

        dst = dst_base if overwrite else dst_base / f"{name}__{timestamp}"
        dst.mkdir(parents=True, exist_ok=True)

        rsync = [
            "rsync",
            "-a",
            "--info=progress2",
            "--human-readable",
            "--exclude=lost+found",
            "--exclude=.Trash-*",
            "--exclude=.Spotlight-*",
        ]

        if verify:
            rsync.append("--checksum")

        if overwrite:
            rsync.append("--inplace")
        else:
            rsync.append("--ignore-existing")

        items = cfg.get("items") or []
        if items:
            for item in items:
                rsync.append(str(safe_join(src_root, item)))
        else:
            rsync.append(str(src_path) + "/")

        rsync.append(str(dst))

        progress_re = re.compile(r"(\d+)%")

        proc = subprocess.Popen(
            rsync, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

        for line in proc.stdout:
            JOB["log"].append(line)
            m = progress_re.search(line)
            if m:
                JOB["progress"] = int(m.group(1))

        proc.wait()
        subprocess.run(["sync"])

        if proc.returncode == 0:
            JOB["result"] = "success"
        elif proc.returncode in (23, 24):
            JOB["result"] = "warning"
        else:
            JOB["result"] = "failed"

    except Exception as e:
        JOB["result"] = "failed"
        JOB["log"].append(str(e))

    JOB["running"] = False
    JOB["finished_at"] = int(time.time())


def _pick_drive(roots: dict[str, Path], include: str, exclude: str | None = None):
    candidates = []
    inc = include.lower()
    exc = exclude.lower() if exclude else None
    for k, v in roots.items():
        lk = k.lower()
        if inc in lk and (not exc or exc not in lk):
            candidates.append((k, v))
    candidates.sort(key=lambda kv: kv[1].stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def auto_backup_check():
    settings = load_settings()
    if not settings.get("auto_backup"):
        return
    if JOB["running"]:
        return

    roots = available_drives()
    if not roots:
        if settings.get("auto_latched"):
            settings["auto_latched"] = None
            save_settings(settings)
        return

    sd = _pick_drive(roots, "sd", "ssd")
    ssd = _pick_drive(roots, "ssd")
    if not sd or not ssd:
        if settings.get("auto_latched"):
            settings["auto_latched"] = None
            save_settings(settings)
        return

    if settings.get("auto_latched"):
        return

    sd_id, sd_path = sd
    ssd_id, _ = ssd
    sd_mtime = int(sd_path.stat().st_mtime)

    last = settings.get("last_auto") or {}
    if last.get("drive") == sd_id and last.get("mtime") == sd_mtime:
        return

    cfg = {
        "src": {"drive": sd_id, "path": ""},
        "dst": {"drive": ssd_id, "path": ""},
        "items": [],
        "backup_name": f"auto_{sd_id}",
        "verify": False,
        "overwrite": False,
    }
    threading.Thread(target=run_backup, args=(cfg,), daemon=True).start()
    settings["last_auto"] = {
        "drive": sd_id,
        "mtime": sd_mtime,
        "dst": ssd_id,
        "started_at": int(time.time()),
    }
    settings["auto_latched"] = {"sd": sd_id, "ssd": ssd_id}
    save_settings(settings)


@router.post("/backup")
def start_backup(cfg: dict, bg: BackgroundTasks):
    if JOB["running"]:
        raise HTTPException(409)
    bg.add_task(run_backup, cfg)
    return {"status": "started"}


@router.get("/backup/status")
def backup_status():
    return JOB


@router.post("/drive/eject/{drive}")
def eject_drive(drive: str):
    roots = available_drives()
    if drive not in roots:
        raise HTTPException(404)
    subprocess.run(["sync"])
    subprocess.run(["umount", str(roots[drive])], check=True)
    return {"status": "ejected"}
