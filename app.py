#!/usr/bin/env python3
"""LAMPP Project Controller — Flask backend (Design A, locked plan)."""
import os
import re
import shutil
import subprocess
import secrets
from datetime import datetime
from pathlib import Path
from functools import wraps

from flask import Flask, request, jsonify, session, redirect, send_file, render_template
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ---------- Config ----------
def _load_dotenv(path: Path):
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass

_load_dotenv(Path(__file__).parent / ".env")

HTDOCS = Path("/opt/lampp/htdocs")
LAMPP_CTL = "/opt/lampp/ctlscript.sh"
MYSQL_BIN = "/opt/lampp/bin/mysql"
ANTIGRAVITY_BIN = "/usr/bin/antigravity-ide"
SUBL_BIN = "/usr/bin/subl"
PORT = int(os.environ.get("PORT", "8888"))

PROTECTED_TOP = {
    "dashboard", "img", "webalizer", "phpmyadmin",
    "applications.html", "bitnami.css", "favicon.ico", "index.php",
}
PROTECTED_DB = {"information_schema", "mysql", "performance_schema", "sys", "phpmyadmin"}
ALLOWED_UPLOAD_EXT = {
    "php", "html", "htm", "css", "js", "png", "jpg", "jpeg", "gif",
    "svg", "ico", "sql", "zip", "md", "txt", "json", "pdf",
}
MAX_UPLOAD_MB = 20

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS_HASH = os.environ.get("ADMIN_PASS_HASH", "")
if not ADMIN_PASS_HASH:
    # default: admin123 (wajib diganti via .env)
    ADMIN_PASS_HASH = generate_password_hash("admin123")

VALID_NAME = re.compile(r"^[A-Za-z0-9._\- ]{1,80}$")
VALID_DB = re.compile(r"^[A-Za-z0-9_$]{1,64}$")


# ---------- Helpers ----------
def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        return fn(*a, **kw)
    return wrapper


def safe_join_htdocs(rel: str) -> Path:
    """Jail semua path ke HTDOCS. rel boleh '' atau 'PAW05/sub'."""
    rel = (rel or "").strip().strip("/")
    if rel in ("", "."):
        return HTDOCS
    # tolak traversal eksplisit
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("path tidak valid")
    target = HTDOCS.joinpath(*parts).resolve()
    # pastikan masih di dalam HTDOCS (resolve symlink juga)
    try:
        target.relative_to(HTDOCS.resolve())
    except ValueError:
        if target != HTDOCS.resolve():
            raise ValueError("path keluar dari htdocs")
    return target


def is_protected_top(rel: str) -> bool:
    top = (rel or "").strip().strip("/").split("/")[0] if (rel or "").strip().strip("/") else ""
    return top in PROTECTED_TOP


def entry_info(p: Path) -> dict:
    st = p.stat()
    size = st.st_size if p.is_file() else dir_size(p)
    return {
        "name": p.name,
        "is_dir": p.is_dir(),
        "size": size,
        "size_h": human_size(size),
        "mtime": st.st_mtime,
        "mtime_h": datetime.fromtimestamp(st.st_mtime).strftime("%d %b %H:%M"),
        "file_count": count_files(p) if p.is_dir() else 0,
    }


def dir_size(p: Path) -> int:
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def count_files(p: Path) -> int:
    try:
        return sum(1 for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def run_ctl(*args, timeout=30):
    cmd = ["sudo", "-n", LAMPP_CTL, *args] if os.geteuid() != 0 else [LAMPP_CTL, *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, "timeout"
    except FileNotFoundError as e:
        return 1, str(e)


def _port_open(port: int, timeout: float = 1.5) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _proc_found(pattern: str) -> bool:
    """pgrep -f bisa lihat proses milik root walau kita non-root."""
    try:
        r = subprocess.run(["pgrep", "-f", pattern],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def _mysql_connect_ok() -> bool:
    try:
        r = subprocess.run([MYSQL_BIN, "-u", "root", "-e", "SELECT 1;"],
                           capture_output=True, text=True, timeout=8)
        return r.returncode == 0
    except Exception:
        return False


def service_status():
    """Gabung 3 sumber (ctlscript + proses + port/koneksi).

    ctlscript.sh status JALAN SEBAGAI non-root tidak bisa diandalkan:
    cek-nya pakai `kill -0 <pid>` yang gagal (EPERM) untuk proses milik
    root + pidfile mysql (mysql:mysql 660) tidak bisa dibaca dhexx.
    Akibatnya selalu lapor 'not running' walau service ON.
    Cross-check via pgrep/port/koneksi memperbaiki itu.
    """
    try:
        r = subprocess.run([LAMPP_CTL, "status"], capture_output=True, text=True, timeout=10)
        out = (r.stdout + r.stderr).lower()
    except Exception as e:
        out = ""
        ctl_err = str(e)
    else:
        ctl_err = ""
    def state(svc):
        # cari baris berisi svc
        for line in out.splitlines():
            if svc in line:
                if "already running" in line or " running" in line and "not running" not in line:
                    return "on"
                if "not running" in line:
                    return "off"
        return "unknown"
    apache = state("apache")
    mysql = state("mysql")
    # fallback keyword
    if apache == "unknown":
        apache = "off" if "apache not running" in out else ("on" if "apache" in out and "running" in out else "unknown")
    if mysql == "unknown":
        mysql = "off" if "mysql not running" in out else ("on" if "mysql" in out and "running" in out else "unknown")

    # cross-check independen (bisa deteksi proses root dari user biasa)
    apache_proc = _proc_found(r"(^|/)httpd|apache2")
    mysql_proc = _proc_found(r"mysqld|mariadbd")
    apache_port = _port_open(80)
    mysql_port = _port_open(3306)
    if apache == "on" or apache_proc or apache_port:
        apache = "on"
    elif apache_proc is False and apache_port is False and apache == "off":
        apache = "off"
    if mysql == "on" or mysql_proc or mysql_port or _mysql_connect_ok():
        mysql = "on"
    elif mysql == "off" and not mysql_proc and not mysql_port:
        mysql = "off"
    raw = out[:2000] + (f"\n[ctl_err: {ctl_err}]" if ctl_err else "")
    return {"apache": apache, "mysql": mysql, "raw": raw}


def mysql_query(sql: str, timeout=15):
    cmd = [MYSQL_BIN, "-u", "root", "-e", sql]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r


# ---------- Pages ----------
@app.get("/")
def index():
    if not session.get("user"):
        return redirect("/login")
    return render_template("index.html")


@app.get("/login")
def login_page():
    if session.get("user"):
        return redirect("/")
    return render_template("login.html")


@app.post("/api/login")
def api_login():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("username") == ADMIN_USER and check_password_hash(ADMIN_PASS_HASH, data.get("password", "")):
        session["user"] = ADMIN_USER
        return jsonify({"ok": True})
    return jsonify({"error": "username/password salah"}), 401


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def api_me():
    return jsonify({"user": session.get("user")})


# ---------- Status & service ----------
@app.get("/api/status")
@login_required
def api_status():
    st = service_status()
    try:
        phpv = subprocess.run([os.path.join("/opt/lampp/bin/php"), "-v"],
                              capture_output=True, text=True, timeout=5)
        php_ver = phpv.stdout.splitlines()[0] if phpv.stdout else "unknown"
    except Exception:
        php_ver = "unknown"
    try:
        items = [p.name for p in HTDOCS.iterdir()]
    except OSError:
        items = []
    return jsonify({
        **st,
        "php": php_ver[:80],
        "htdocs": str(HTDOCS),
        "project_count": sum(1 for n in items if (HTDOCS / n).is_dir() and n not in PROTECTED_TOP),
        "time": datetime.now().strftime("%d %b %H:%M"),
    })


@app.post("/api/service/<svc>/<action>")
@login_required
def api_service(svc, action):
    if svc not in ("apache", "mysql"):
        return jsonify({"error": "service tidak dikenal"}), 400
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "aksi tidak dikenal"}), 400
    code, out = run_ctl(action, svc)
    # ctlscript pakai format: ./ctlscript.sh start apache  ATAU  start mysql?
    # XAMPP lampp: 'start apache' / 'start mysql'. Coba fallback urutan argumen.
    if code != 0 and "unknown" in out.lower():
        code, out = run_ctl(svc, action)
    return jsonify({"ok": code == 0, "output": out[-2000:], "status": service_status()})


# ---------- Projects ----------
@app.get("/api/projects")
@login_required
def api_projects():
    q = (request.args.get("q") or "").lower()
    items = []
    try:
        for p in sorted(HTDOCS.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_dir():
                continue
            if q and q not in p.name.lower():
                continue
            info = entry_info(p)
            info["protected"] = p.name in PROTECTED_TOP
            info["url"] = f"http://localhost/{p.name}"
            items.append(info)
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"items": items})


@app.post("/api/projects")
@login_required
def api_projects_create():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    template = data.get("template", "empty")
    if not VALID_NAME.match(name):
        return jsonify({"error": "nama project tidak valid (huruf/angka/spasi/._- max 80)"}), 400
    dest = HTDOCS / name
    if dest.exists():
        return jsonify({"error": "project sudah ada"}), 400
    dest.mkdir(parents=False)
    if template == "php":
        (dest / "index.php").write_text(
            "<?php\n// " + name + "\n?>\n<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
            f"<title>{name}</title></head><body><h1>{name}</h1></body></html>\n")
        (dest / "koneksi.php").write_text(
            "<?php\n$host='localhost';$user='root';$pass='';$db='';\n"
            "$conn = mysqli_connect($host,$user,$pass,$db);\n"
            "if(!$conn){die('Koneksi gagal: '.mysqli_connect_error());}\n")
    elif template == "html":
        (dest / "index.html").write_text(
            f"<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\"><title>{name}</title></head>"
            f"<body><h1>{name}</h1></body></html>\n")
    else:
        (dest / "index.php").write_text(f"<?php // {name}\n")
    return jsonify({"ok": True, "info": entry_info(dest)})


@app.post("/api/projects/<name>/rename")
@login_required
def api_project_rename(name):
    data = request.get_json(force=True, silent=True) or {}
    new = (data.get("newName") or "").strip()
    if name in PROTECTED_TOP:
        return jsonify({"error": "folder sistem tidak boleh di-rename"}), 403
    if not VALID_NAME.match(new):
        return jsonify({"error": "nama baru tidak valid"}), 400
    src = HTDOCS / name
    dst = HTDOCS / new
    if not src.is_dir():
        return jsonify({"error": "project tidak ditemukan"}), 404
    if dst.exists():
        return jsonify({"error": "nama tujuan sudah ada"}), 400
    src.rename(dst)
    return jsonify({"ok": True})


@app.post("/api/projects/<name>/duplicate")
@login_required
def api_project_duplicate(name):
    src = HTDOCS / name
    if not src.is_dir():
        return jsonify({"error": "project tidak ditemukan"}), 404
    base = f"{name}-copy"
    dst = HTDOCS / base
    i = 2
    while dst.exists():
        dst = HTDOCS / f"{base}-{i}"
        i += 1
    shutil.copytree(src, dst)
    return jsonify({"ok": True, "name": dst.name})


@app.delete("/api/projects/<name>")
@login_required
def api_project_delete(name):
    if name in PROTECTED_TOP:
        return jsonify({"error": "folder sistem tidak boleh dihapus"}), 403
    target = HTDOCS / name
    if not target.is_dir():
        return jsonify({"error": "project tidak ditemukan"}), 404
    shutil.rmtree(target)
    return jsonify({"ok": True})


# ---------- Files ----------
@app.get("/api/files")
@login_required
def api_files():
    rel = request.args.get("path", "")
    q = (request.args.get("q") or "").lower()
    try:
        target = safe_join_htdocs(rel)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not target.exists():
        return jsonify({"error": "path tidak ditemukan"}), 404
    if target.is_file():
        return jsonify({"file": entry_info(target)})
    items = []
    try:
        for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if q and q not in p.name.lower():
                continue
            items.append(entry_info(p))
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    # breadcrumb
    parts = [p for p in (rel or "").strip().strip("/").split("/") if p] if rel.strip().strip("/") else []
    return jsonify({
        "path": rel.strip().strip("/"),
        "breadcrumb": parts,
        "is_htdocs": len(parts) == 0,
        "items": items,
    })


@app.post("/api/files")
@login_required
def api_files_create():
    data = request.get_json(force=True, silent=True) or {}
    rel = data.get("path", "")
    name = (data.get("name") or "").strip()
    kind = data.get("type", "file")
    template = data.get("template", "empty")
    if not VALID_NAME.match(name):
        return jsonify({"error": "nama file tidak valid"}), 400
    try:
        parent = safe_join_htdocs(rel)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if is_protected_top(rel) and len((rel or "").strip().strip("/").split("/")) <= 1 and rel.strip().strip("/"):
        return jsonify({"error": "folder sistem dilindungi"}), 403
    if not parent.is_dir():
        return jsonify({"error": "folder tujuan tidak ditemukan"}), 404
    dest = parent / secure_filename(name) if "/" not in name else parent / name
    # izinkan .php dll tapi cegah traversal
    try:
        dest.resolve().relative_to(HTDOCS.resolve())
    except ValueError:
        return jsonify({"error": "path keluar dari htdocs"}), 400
    if dest.exists():
        return jsonify({"error": "sudah ada"}), 400
    if kind == "dir":
        dest.mkdir()
    else:
        content = ""
        if template == "php":
            content = f"<?php // {name}\n"
        elif template == "html":
            content = ("<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
                       f"<title>{name}</title></head><body></body></html>\n")
        elif template == "koneksi":
            content = ("<?php\n$host='localhost';$user='root';$pass='';$db='';\n"
                       "$conn = mysqli_connect($host,$user,$pass,$db);\n"
                       "if(!$conn){die('Koneksi gagal: '.mysqli_connect_error());}\n")
        dest.write_text(content)
    return jsonify({"ok": True, "info": entry_info(dest)})


@app.put("/api/files")
@login_required
def api_files_rename():
    data = request.get_json(force=True, silent=True) or {}
    rel = (data.get("path") or "").strip().strip("/")
    new = (data.get("newName") or "").strip()
    if not rel:
        return jsonify({"error": "tidak bisa rename htdocs"}), 400
    if is_protected_top(rel):
        return jsonify({"error": "file/folder sistem dilindungi"}), 403
    if not VALID_NAME.match(new):
        return jsonify({"error": "nama baru tidak valid"}), 400
    try:
        src = safe_join_htdocs(rel)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not src.exists():
        return jsonify({"error": "tidak ditemukan"}), 404
    dst = src.parent / new
    if dst.exists():
        return jsonify({"error": "nama tujuan sudah ada"}), 400
    src.rename(dst)
    return jsonify({"ok": True})


@app.delete("/api/files")
@login_required
def api_files_delete():
    rel = (request.args.get("path") or "").strip().strip("/")
    if not rel:
        return jsonify({"error": "tidak bisa hapus htdocs"}), 400
    if is_protected_top(rel):
        return jsonify({"error": "file/folder sistem dilindungi"}), 403
    try:
        target = safe_join_htdocs(rel)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not target.exists():
        return jsonify({"error": "tidak ditemukan"}), 404
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return jsonify({"ok": True})


@app.post("/api/upload")
@login_required
def api_upload():
    rel = request.form.get("path", "")
    try:
        dest_dir = safe_join_htdocs(rel)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not dest_dir.is_dir():
        return jsonify({"error": "folder tujuan tidak ditemukan"}), 404
    if is_protected_top(rel) and rel.strip().strip("/"):
        return jsonify({"error": "folder sistem dilindungi"}), 403
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "tidak ada file"}), 400
    saved = []
    for f in files:
        fname = secure_filename(f.filename or "")
        if not fname or "." not in fname:
            continue
        ext = fname.rsplit(".", 1)[-1].lower()
        if ext not in ALLOWED_UPLOAD_EXT:
            continue
        out = dest_dir / fname
        i = 2
        while out.exists():
            stem = fname.rsplit(".", 1)[0]
            out = dest_dir / f"{stem}-{i}.{ext}"
            i += 1
        f.save(str(out))
        saved.append(fname)
    return jsonify({"ok": True, "saved": saved})


@app.get("/api/download")
@login_required
def api_download():
    rel = (request.args.get("path") or "").strip().strip("/")
    try:
        target = safe_join_htdocs(rel)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not target.is_file():
        return jsonify({"error": "bukan file"}), 404
    return send_file(str(target), as_attachment=True)


# ---------- Open in editor ----------
@app.post("/api/open")
@login_required
def api_open():
    data = request.get_json(force=True, silent=True) or {}
    rel = (data.get("path") or "").strip().strip("/")
    editor = data.get("editor", "antigravity")
    try:
        target = safe_join_htdocs(rel)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not target.exists():
        return jsonify({"error": "tidak ditemukan"}), 404
    binary = ANTIGRAVITY_BIN if editor == "antigravity" else SUBL_BIN
    if not os.path.exists(binary):
        return jsonify({"error": f"editor tidak ditemukan: {binary}"}), 500
    env = dict(os.environ)
    try:
        subprocess.Popen([binary, str(target)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


# ---------- Databases ----------
@app.get("/api/databases")
@login_required
def api_db_list():
    try:
        r = mysql_query("SHOW DATABASES;")
    except FileNotFoundError:
        return jsonify({"error": "mysql client tidak ditemukan", "running": False}), 500
    except Exception as e:
        return jsonify({"error": str(e), "running": False}), 500
    if r.returncode != 0:
        return jsonify({"error": r.stderr.strip()[-500:] or "mysql mati / akses ditolak",
                        "running": False, "items": []}), 200
    dbs = [l.strip() for l in r.stdout.splitlines()[1:] if l.strip()]
    q = (request.args.get("q") or "").lower()
    items = []
    for db in dbs:
        if q and q not in db.lower():
            continue
        # hitung tabel
        try:
            t = mysql_query(f"SHOW TABLES IN `{db}`;")
            tables = len(t.stdout.splitlines()) - 1 if t.returncode == 0 else 0
        except Exception:
            tables = 0
        items.append({"name": db, "tables": max(tables, 0),
                      "protected": db in PROTECTED_DB,
                      "phpmyadmin": f"http://localhost/phpmyadmin/index.php?route=/database/structure&db={db}"})
    return jsonify({"items": items, "running": True})


@app.post("/api/databases")
@login_required
def api_db_create():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not VALID_DB.match(name):
        return jsonify({"error": "nama DB tidak valid (huruf/angka/_/$)"}), 400
    r = mysql_query(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4;")
    if r.returncode != 0:
        return jsonify({"error": r.stderr.strip()[-500:]}), 400
    return jsonify({"ok": True})


@app.delete("/api/databases/<name>")
@login_required
def api_db_drop(name):
    if name in PROTECTED_DB:
        return jsonify({"error": "database sistem dilindungi"}), 403
    r = mysql_query(f"DROP DATABASE `{name}`;")
    if r.returncode != 0:
        return jsonify({"error": r.stderr.strip()[-500:]}), 400
    return jsonify({"ok": True})


@app.post("/api/import")
@login_required
def api_db_import():
    db = (request.form.get("db") or "").strip()
    f = request.files.get("file")
    if not VALID_DB.match(db):
        return jsonify({"error": "nama DB tidak valid"}), 400
    if not f or not f.filename.endswith(".sql"):
        return jsonify({"error": "upload file .sql"}), 400
    tmp = Path("/tmp") / f"lampp-import-{secrets.token_hex(4)}.sql"
    f.save(str(tmp))
    try:
        with open(tmp, "r") as fh:
            r = subprocess.run([MYSQL_BIN, "-u", "root", db],
                               stdin=fh, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return jsonify({"error": r.stderr.strip()[-1000:]}), 400
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return jsonify({"ok": True})


if __name__ == "__main__":
    print(f"LAMPP Manager -> http://localhost:{PORT}  (htdocs={HTDOCS})")
    app.run(host="127.0.0.1", port=PORT, debug=False)
