import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import urllib.request
import urllib.parse

from flask import Flask, abort, jsonify, request, render_template

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
PROFILES_DIR = BASE_DIR / "profiles_data"
EXT_DIR      = BASE_DIR / "extensions"

PROFILES_DIR.mkdir(exist_ok=True)
EXT_DIR.mkdir(exist_ok=True)

# { profile_id: pid }
running_processes: dict[str, int] = {}
_procs: dict[str, subprocess.Popen] = {}
# { profile_id: file handle } — kept open while browser runs, closed in watcher
_log_files: dict[str, object] = {}

AMO_API = "https://addons.mozilla.org/api/v5"
AMO_HEADERS = {
    "User-Agent": "CamoufoxManager/1.0 (profile manager; contact@localhost)",
}

# ── Process watcher ───────────────────────────────────────────────────────────
def _watch_process(profile_id: str, proc: subprocess.Popen):
    """Wait for browser to exit, then save open tabs and clean up."""
    proc.wait()

    # Close the log file handle now that the process has exited
    lf = _log_files.pop(profile_id, None)
    try:
        if lf:
            lf.close()
    except Exception:
        pass

    # Read log file — our launch script prints open URLs before exiting
    try:
        log_path = _profile_path(profile_id) / "browser.log"
        if log_path.exists():
            raw = log_path.read_text(errors="replace")
            urls = []
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("OPEN_URL:"):
                    url = line[len("OPEN_URL:"):].strip()
                    if url and url not in ("about:blank", "about:newtab"):
                        urls.append(url)
            if urls:
                # Persist to meta
                meta_p = _meta_path(profile_id)
                if meta_p.exists():
                    with open(meta_p) as f:
                        meta = json.load(f)
                    meta["last_urls"] = urls
                    meta["updated_at"] = datetime.utcnow().isoformat()
                    with open(meta_p, "w") as f:
                        json.dump(meta, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

    running_processes.pop(profile_id, None)
    _procs.pop(profile_id, None)


# ── Profile helpers ───────────────────────────────────────────────────────────
def _profile_path(profile_id: str) -> Path:
    return PROFILES_DIR / profile_id

def _meta_path(profile_id: str) -> Path:
    return _profile_path(profile_id) / "meta.json"

def _load_meta(profile_id: str) -> dict:
    p = _meta_path(profile_id)
    if not p.exists():
        abort(404, description=f"Profile {profile_id} not found")
    with open(p) as f:
        return json.load(f)

def _save_meta(profile_id: str, meta: dict):
    with open(_meta_path(profile_id), "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

def _list_profiles() -> list[dict]:
    profiles = []
    for d in PROFILES_DIR.iterdir():
        if d.is_dir():
            meta_file = d / "meta.json"
            if meta_file.exists():
                with open(meta_file) as f:
                    meta = json.load(f)
                meta["is_running"] = meta["id"] in running_processes
                profiles.append(meta)
    profiles.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return profiles

def _is_running(profile_id: str) -> bool:
    if profile_id not in running_processes:
        return False
    pid = running_processes[profile_id]
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        del running_processes[profile_id]
        return False


# ── Extension helpers ─────────────────────────────────────────────────────────
def _list_local_extensions() -> list[dict]:
    result = []
    for xpi in sorted(EXT_DIR.glob("*.xpi")):
        stat = xpi.stat()
        result.append({
            "filename": xpi.name,
            "size":     stat.st_size,
            "size_kb":  round(stat.st_size / 1024, 1),
            "mtime":    datetime.utcfromtimestamp(stat.st_mtime).isoformat(),
        })
    return result

def _safe_filename(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-.]", "_", name)

def _amo_get(path: str, params: dict | None = None) -> dict:
    url = f"{AMO_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=AMO_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


# ── Launch script builder ─────────────────────────────────────────────────────
def _build_camoufox_script(profile_id: str, meta: dict, goto_url: str = "") -> str:
    """
    Generate the Python launch script for this profile.

    goto_url: if non-empty, opens this URL instead of the configured launch_url
              or the remembered last_urls.  Used for "Add Extension → AMO" flow.
    """
    profile_dir = _profile_path(profile_id).resolve()

    os_val  = meta.get("os", "windows")
    locale  = meta.get("locale", "en-US")
    cpu_val      = (meta.get("fingerprint") or {}).get("cpu", "auto")
    timezone_val = (meta.get("fingerprint") or {}).get("timezone", "auto")

    # ── Decide what URLs to open ──────────────────────────────────────────────
    if goto_url:
        # Explicit override (e.g. AMO store URL)
        open_urls = [goto_url]
    else:
        configured = (meta.get("fingerprint") or {}).get("launch_url", "").strip()
        remembered = meta.get("last_urls", [])
        if remembered:
            open_urls = remembered          # restore last session
        elif configured:
            open_urls = [configured]
        else:
            open_urls = ["about:blank"]

    # ── Extensions ────────────────────────────────────────────────────────────
    enabled_exts: list[str] = meta.get("extensions", [])
    ext_paths = []
    for fname in enabled_exts:
        xpi = EXT_DIR / fname
        if xpi.exists():
            ext_paths.append(str(xpi.resolve()))

    _OS_FONT_KEY = {"windows": "win", "macos": "mac", "linux": "lin"}
    font_os_key = _OS_FONT_KEY.get(os_val, "win")

    # ── Build arg_lines ───────────────────────────────────────────────────────
    arg_lines = [
        f'        os="{os_val}"',
        '        humanize=False',
        '        i_know_what_im_doing=True',
        f'        locale=["{locale}"]',
        '        persistent_context=True',
        f'        user_data_dir="{profile_dir}/userdata"',
        '        block_webrtc=True',
        '        fonts=_os_fonts',
        '        custom_fonts_only=True',
    ]

    if timezone_val == "auto":
        arg_lines.append('        geoip=True')

    if ext_paths:
        paths_repr = "[" + ", ".join(f'"{p}"' for p in ext_paths) + "]"
        arg_lines.append(f'        extensions={paths_repr}')

    config_inner = [
        '            "showcursor": False',
        '            "disableTheming": True',
        '            "window.outerWidth": 1920',
        '            "window.outerHeight": 1080',
        '            "window.innerWidth": 1920',
        '            "window.innerHeight": 1032',
        '            "window.screenX": 0',
        '            "window.screenY": 0',
        '            "screen.width": 1920',
        '            "screen.height": 1080',
        '            "screen.availWidth": 1920',
        '            "screen.availHeight": 1032',
    ]
    if cpu_val and cpu_val != "auto":
        config_inner.append(f'            "navigator.hardwareConcurrency": {int(cpu_val)}')
    if timezone_val and timezone_val != "auto":
        config_inner.append(f'            "timezone": "{timezone_val}"')

    config_block = "        config={\n" + ",\n".join(config_inner) + ",\n        }"
    arg_lines.append(config_block)


# ── 替换下面这段 ──
    proxy_val = meta.get("proxy", "").strip()
    if proxy_val.startswith("socks5://"):
        proxy_val = proxy_val.replace("socks5://", "socks5h://") # 强制远端解析 DNS
        
    if proxy_val:
        arg_lines.append(f'        proxy="{proxy_val}"')
    # ── 替换结束 ──



    arg_lines.append('        headless=False')
    arg_lines.append('        env=_browser_env')   # 直接传给 Playwright，确保 Firefox 收到 IM 变量

    args_str = ",\n".join(arg_lines)

    # ── Build open-tabs code ──────────────────────────────────────────────────
    open_lines = []
    open_lines.append(f'        await page.goto({repr(open_urls[0])})')
    for extra_url in open_urls[1:]:
        open_lines.append(f'        extra = await context.new_page()')
        open_lines.append(f'        await extra.goto({repr(extra_url)})')
    open_tabs_code = "\n".join(open_lines)

    script = f'''#!/usr/bin/env python3
import asyncio
import json
import os
import sys

from camoufox.async_api import AsyncCamoufox

# ── 跨平台输入法支持 ──────────────────────────────────────────────────────────
# 直接通过 env= 传给 AsyncCamoufox/Playwright，确保 Firefox 进程确实收到这些变量，
# 而不是依赖不确定的 os.environ 继承链（Playwright 内部可能重建浏览器的环境）。
#
# 关键结论（来自 fcitx5/Firefox issue #595, #789 及 ArchWiki）：
#   · Wayland 原生 Firefox (MOZ_ENABLE_WAYLAND=1)：fcitx5 通过 zwp_text_input 协议
#     与 Firefox 通信，不需要 GTK_IM_MODULE，设了反而可能干扰。
#   · X11 / XWayland Firefox：GTK_IM_MODULE=fcitx 与 Firefox 有已知兼容性问题；
#     应改用 GTK_IM_MODULE=xim + XMODIFIERS=@im=fcitx（XIM 协议），Firefox 原生支持。
#   · Windows / macOS：系统级 IME 框架（TSF / 系统输入法）透明接管，无需配置。
if sys.platform.startswith("linux"):
    def _detect_im_name():
        """探测当前运行的输入法框架，返回 XMODIFIERS 用的名字。"""
        import shutil, subprocess as _sp
        try:
            _procs = set(_sp.check_output(
                ["ps", "-e", "-o", "comm="], stderr=_sp.DEVNULL
            ).decode().split())
            if "fcitx5" in _procs or "fcitx" in _procs: return "fcitx"
            if "ibus-daemon"                in _procs:   return "ibus"
        except Exception:
            pass
        if shutil.which("fcitx5") or shutil.which("fcitx"): return "fcitx"
        if shutil.which("ibus"):                             return "ibus"
        return None

    _browser_env = dict(os.environ)  # 复制完整环境（含 DBUS_* / XDG_* / DISPLAY 等）
    _im_name = _detect_im_name()

    if "WAYLAND_DISPLAY" in _browser_env:
        # ── Wayland 原生模式 ──────────────────────────────────────────────────
        # Firefox 通过 Wayland text-input 协议与 fcitx5 通信，不依赖 GTK_IM_MODULE。
        # 移除可能被父进程错误设置的 GTK_IM_MODULE，避免干扰 Wayland 协议。
        _browser_env.pop("GTK_IM_MODULE", None)
        _browser_env["MOZ_ENABLE_WAYLAND"] = "1"
        if _im_name:
            _browser_env.setdefault("XMODIFIERS", f"@im={{_im_name}}")
    else:
        # ── X11 / XWayland 模式 ───────────────────────────────────────────────
        # GTK_IM_MODULE=xim 让 GTK 走 XIM 协议，Firefox 对 XIM 的支持比 fcitx 模块好。
        # XMODIFIERS 告知 XIM 服务器名（即 fcitx/ibus），两者配合完成输入法通信。
        _browser_env["GTK_IM_MODULE"] = "xim"
        if _im_name:
            _browser_env["XMODIFIERS"]    = f"@im={{_im_name}}"
            _browser_env["QT_IM_MODULE"]  = _im_name
else:
    _browser_env = None   # Windows / macOS：不传 env，使用 Playwright 默认行为
# ── 输入法支持结束 ────────────────────────────────────────────────────────────

# 字体防泄露：从 camoufox 自带的 fonts.json 读取目标 OS 的标准字体列表，
# 配合 custom_fonts_only=True 确保真实系统字体不会泄露给网站。
_camoufox_fonts_path = os.path.join(os.path.dirname(__import__("camoufox").__file__), "fonts.json")
with open(_camoufox_fonts_path, "rb") as _f:
    _os_fonts = json.loads(_f.read()).get("{font_os_key}", [])

async def main():
    async with AsyncCamoufox(
{args_str},
    ) as context:
        pages = context.pages
        page = pages[0] if pages else await context.new_page()
{open_tabs_code}
        print("CAMOUFOX_READY", flush=True)

        async def _on_close():
            for p in context.pages:
                try:
                    print(f"OPEN_URL:{{p.url}}", flush=True)
                except Exception:
                    pass
        context.on("close", lambda: asyncio.ensure_future(_on_close()))

        # timeout=0 = 无限等待，不传则默认 30 秒后抛 TimeoutError
        # 导致 async with 块退出 → context manager 关闭浏览器 → 窗口闪退
        await context.wait_for_event("close", timeout=0)

asyncio.run(main())
'''
    return script


# ── Routes: UI ────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ── Routes: Profiles API ──────────────────────────────────────────────────────
@app.route("/api/profiles", methods=["GET"])
def list_profiles():
    profiles = _list_profiles()
    for p in profiles:
        p["is_running"] = _is_running(p["id"])
    return jsonify(profiles)


@app.route("/api/profiles", methods=["POST"])
def create_profile():
    data = request.get_json(force=True)
    profile_id = str(uuid.uuid4())[:8]

    profile_dir = _profile_path(profile_id)
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "userdata").mkdir(exist_ok=True)

    meta = {
        "id":         profile_id,
        "name":       data.get("name", f"Profile {profile_id}"),
        "os":         data.get("os", "windows"),
        "locale":     data.get("locale", "en-US"),
        "proxy":      data.get("proxy", ""),
        "tags":       data.get("tags", []),
        "notes":      data.get("notes", ""),
        "extensions": [],
        "last_urls":  [],          # session memory
        "fingerprint": {
            "screen": data.get("screen", "1920x1080"),
        },
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }

    _save_meta(profile_id, meta)
    return jsonify(meta), 201


@app.route("/api/profiles/<profile_id>", methods=["GET"])
def get_profile(profile_id):
    meta = _load_meta(profile_id)
    meta["is_running"] = _is_running(profile_id)
    return jsonify(meta)


@app.route("/api/profiles/<profile_id>", methods=["PUT"])
def update_profile(profile_id):
    meta = _load_meta(profile_id)
    data = request.get_json(force=True)

    updatable = [
        "name", "os", "locale", "proxy", "tags", "notes",
        "fingerprint", "screen", "gpu", "cpu", "timezone", "launch_url",
        "extensions", "last_urls",
    ]
    for key in updatable:
        if key in data:
            if key in ("screen", "gpu", "cpu", "timezone", "launch_url"):
                meta.setdefault("fingerprint", {})[key] = data[key]
            else:
                meta[key] = data[key]

    meta["updated_at"] = datetime.utcnow().isoformat()
    _save_meta(profile_id, meta)
    return jsonify(meta)


@app.route("/api/profiles/<profile_id>", methods=["DELETE"])
def delete_profile(profile_id):
    if _is_running(profile_id):
        return jsonify({"error": "Profile is running. Stop it first."}), 400

    profile_dir = _profile_path(profile_id)
    if profile_dir.exists():
        shutil.rmtree(profile_dir)

    return jsonify({"ok": True})


# ── Routes: Extensions ────────────────────────────────────────────────────────
@app.route("/api/extensions", methods=["GET"])
def list_extensions():
    return jsonify(_list_local_extensions())


@app.route("/api/extensions/search", methods=["POST"])
def search_extensions():
    data  = request.get_json(force=True)
    query = (data.get("q") or "").strip()
    page  = int(data.get("page", 1))

    if not query:
        return jsonify({"error": "q is required"}), 400

    try:
        resp = _amo_get("/addons/search/", {
            "q":         query,
            "type":      "extension",
            "sort":      "users",
            "page":      page,
            "page_size": 12,
            "lang":      "en-US",
            "app":       "firefox",
        })
    except Exception as e:
        return jsonify({"error": f"AMO API error: {e}"}), 502

    addons = []
    for item in resp.get("results", []):
        current_version = item.get("current_version") or {}
        files = current_version.get("files") or []
        download_url = next(
            (f.get("url") for f in files if (f.get("url") or "").endswith(".xpi")),
            None
        )
        if not download_url:
            continue

        slug     = item.get("slug", "")
        version  = current_version.get("version", "")
        filename = _safe_filename(f"{slug}-{version}.xpi")
        already  = (EXT_DIR / filename).exists()

        icon_url = next(
            (item.get("icons", {}).get(s) for s in ("64", "32", "16") if item.get("icons", {}).get(s)),
            None
        )

        addons.append({
            "addon_id":      item.get("guid") or item.get("id"),
            "slug":          slug,
            "name":          (item.get("name") or {}).get("en-US", slug),
            "summary":       (item.get("summary") or {}).get("en-US", ""),
            "icon":          icon_url,
            "version":       version,
            "users":         item.get("average_daily_users", 0),
            "rating":        item.get("ratings", {}).get("average", 0),
            "download_url":  download_url,
            "filename":      filename,
            "already_local": already,
        })

    return jsonify({"count": resp.get("count", 0), "page": page, "results": addons})


@app.route("/api/extensions/install", methods=["POST"])
def install_extension():
    data         = request.get_json(force=True)
    download_url = (data.get("download_url") or "").strip()
    filename     = (data.get("filename") or "").strip()

    if not download_url or not filename:
        return jsonify({"error": "download_url and filename are required"}), 400

    filename = _safe_filename(filename)
    if not filename.endswith(".xpi"):
        filename += ".xpi"

    dest = EXT_DIR / filename
    if dest.exists():
        return jsonify({"ok": True, "filename": filename, "cached": True})

    parsed = urllib.parse.urlparse(download_url)
    allowed_hosts = {
        "addons.mozilla.org",
        "addons.cdn.mozilla.net",
        "addons-amo-cdn.prod.webservices.mozgcp.net",
    }
    if parsed.hostname not in allowed_hosts:
        return jsonify({"error": "URL not from an allowed AMO host"}), 400

    try:
        req = urllib.request.Request(download_url, headers=AMO_HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data_bytes = resp.read()
        if data_bytes[:2] != b"PK":
            return jsonify({"error": "Downloaded file is not a valid .xpi"}), 502
        dest.write_bytes(data_bytes)
    except Exception as e:
        return jsonify({"error": f"Download failed: {e}"}), 502

    return jsonify({"ok": True, "filename": filename, "cached": False, "size": dest.stat().st_size})


@app.route("/api/extensions/<filename>", methods=["DELETE"])
def delete_extension(filename: str):
    filename = _safe_filename(filename)
    dest = EXT_DIR / filename
    if not dest.exists():
        return jsonify({"error": "File not found"}), 404

    dest.unlink()

    for d in PROFILES_DIR.iterdir():
        meta_file = d / "meta.json"
        if meta_file.exists():
            with open(meta_file) as f:
                meta = json.load(f)
            if filename in meta.get("extensions", []):
                meta["extensions"] = [e for e in meta["extensions"] if e != filename]
                meta["updated_at"] = datetime.utcnow().isoformat()
                with open(meta_file, "w") as f:
                    json.dump(meta, f, indent=2, ensure_ascii=False)

    return jsonify({"ok": True})


@app.route("/api/extensions/<filename>/profiles", methods=["GET"])
def extension_profiles(filename: str):
    filename = _safe_filename(filename)
    using = []
    for d in PROFILES_DIR.iterdir():
        meta_file = d / "meta.json"
        if meta_file.exists():
            with open(meta_file) as f:
                meta = json.load(f)
            if filename in meta.get("extensions", []):
                using.append({"id": meta["id"], "name": meta.get("name", meta["id"])})
    return jsonify(using)


@app.route("/api/profiles/<profile_id>/extensions", methods=["PUT"])
def update_profile_extensions(profile_id: str):
    meta  = _load_meta(profile_id)
    data  = request.get_json(force=True)
    names = data.get("extensions", [])

    valid = [_safe_filename(n) for n in names if (EXT_DIR / _safe_filename(n)).exists()]
    meta["extensions"] = valid
    meta["updated_at"] = datetime.utcnow().isoformat()
    _save_meta(profile_id, meta)
    return jsonify({"ok": True, "extensions": valid})


# ── Routes: Session memory ────────────────────────────────────────────────────
@app.route("/api/profiles/<profile_id>/last-urls", methods=["DELETE"])
def clear_last_urls(profile_id: str):
    """Clear the remembered session URLs for this profile."""
    meta = _load_meta(profile_id)
    meta["last_urls"] = []
    meta["updated_at"] = datetime.utcnow().isoformat()
    _save_meta(profile_id, meta)
    return jsonify({"ok": True})


# ── Routes: Proxy test ────────────────────────────────────────────────────────
@app.route("/api/proxy-test", methods=["POST"])
def proxy_test():
    data      = request.get_json(force=True)
    proxy_url = (data.get("proxy") or "").strip()

    try:
        t0  = time.time()
        cmd = ["curl", "-s", "-m", "15", "--max-time", "15"]
        if proxy_url:
            cmd += ["-x", proxy_url]
        cmd.append("https://ipinfo.io/json")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)

        if result.returncode != 0:
            return jsonify({
                "ok":    False,
                "error": f"Connection failed (curl exit {result.returncode}): {result.stderr.strip()}",
            })

        latency_ms = int((time.time() - t0) * 1000)
        info = json.loads(result.stdout)

        return jsonify({
            "ok":      True,
            "ip":      info.get("ip",     "?"),
            "city":    info.get("city",   "?"),
            "region":  info.get("region", "?"),
            "country": info.get("country","?"),
            "org":     info.get("org",    "?"),
            "latency": latency_ms,
        })

    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Connection timed out"})
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": "Invalid response from IP detection service"})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "curl not found — please install curl"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── Routes: Launch / Stop ─────────────────────────────────────────────────────
@app.route("/api/profiles/<profile_id>/launch", methods=["POST"])
def launch_profile(profile_id):
    if _is_running(profile_id):
        return jsonify({"error": "Already running"}), 400

    data     = request.get_json(force=True) if request.content_length else {}
    # Optional: caller may pass { "goto_url": "https://..." } to override open URL
    goto_url = (data.get("goto_url") or "").strip() if data else ""

    meta           = _load_meta(profile_id)
    script_content = _build_camoufox_script(profile_id, meta, goto_url=goto_url)

    script_path = _profile_path(profile_id) / "launch.py"
    with open(script_path, "w") as f:
        f.write(script_content)

    # ── FIX: redirect stdout+stderr to a log file instead of PIPE ────────────
    # Using subprocess.PIPE causes the 64 KB OS pipe buffer to fill up from
    # Firefox/camoufox log output. Since _watch_process only reads the pipe
    # *after* the process exits, the buffer eventually blocks the child's
    # write() calls, freezing the asyncio event loop inside launch.py, which
    # causes camoufox to kill itself — manifesting as a sudden window crash.
    # Writing to a file has no buffer limit and never blocks.
    log_path = _profile_path(profile_id) / "browser.log"

    # ── 构建子进程环境（保留桌面会话变量，IM 由生成脚本直接传给 AsyncCamoufox）──
    # GTK_IM_MODULE 的正确值因 X11/Wayland 而异，由 launch.py 在运行时判断并
    # 通过 env= 直接传给 AsyncCamoufox/Playwright，以绕过继承链的不确定性。
    # 此处只确保 DISPLAY / WAYLAND_DISPLAY / DBUS_* / XDG_* 等会话变量存在。
    launch_env = os.environ.copy()

    if os.name != "nt":
        if "DISPLAY" not in launch_env and "WAYLAND_DISPLAY" not in launch_env:
            launch_env["DISPLAY"] = ":0"   # 兜底，避免无头模式被意外触发
    # ── 环境变量构建结束 ──────────────────────────────────────────────────────

    try:
        log_file = open(log_path, "w")

        proc = subprocess.Popen(
            ["python3", str(script_path)],
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
            env=launch_env,
        )

        time.sleep(1.5)
        if proc.poll() is not None:
            log_file.close()
            # Read whatever was logged to get the error detail
            detail = "Unknown error"
            try:
                detail = log_path.read_text(errors="replace").strip() or detail
            except Exception:
                pass
            return jsonify({"error": f"Browser failed to start: {detail}"}), 500

        running_processes[profile_id] = proc.pid
        _procs[profile_id]            = proc
        _log_files[profile_id]        = log_file  # watcher will close it on exit

        threading.Thread(
            target=_watch_process, args=(profile_id, proc), daemon=True
        ).start()
        return jsonify({"ok": True, "pid": proc.pid})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/profiles/<profile_id>/stop", methods=["POST"])
def stop_profile(profile_id):
    if not _is_running(profile_id):
        return jsonify({"error": "Not running"}), 400

    pid = running_processes[profile_id]
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    del running_processes[profile_id]
    _procs.pop(profile_id, None)
    return jsonify({"ok": True})


@app.route("/api/profiles/<profile_id>/status", methods=["GET"])
def profile_status(profile_id):
    return jsonify({"is_running": _is_running(profile_id)})


@app.route("/api/running", methods=["GET"])
def all_running():
    alive = {pid: _is_running(pid) for pid in list(running_processes)}
    return jsonify({k: v for k, v in alive.items() if v})


# ── Routes: Zombie killer ─────────────────────────────────────────────────────
@app.route("/api/zombie-kill", methods=["POST"])
def zombie_kill():
    """Kill untracked camoufox/firefox processes."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "camoufox"],
            capture_output=True, text=True
        )
        tracked_pids = set(running_processes.values())
        killed = []
        for line in result.stdout.splitlines():
            try:
                pid = int(line.strip())
                if pid not in tracked_pids:
                    os.kill(pid, signal.SIGTERM)
                    killed.append(pid)
            except Exception:
                pass
        return jsonify({"ok": True, "killed": killed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("🦊 Camoufox Manager running at http://localhost:7070")
    app.run(host="127.0.0.1", port=7070, debug=False)
