from __future__ import annotations
import os, stat, subprocess, time, re, shutil
from pathlib import Path
from fastapi import APIRouter, HTTPException, BackgroundTasks

router = APIRouter(prefix="/api", tags=["backup"])

MEDIA_ROOT = Path("/media/admin")

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
