# -*- coding: utf-8 -*-
"""
Minecraft 整合包更新器 - 简化版核心引擎
功能：对比两个整合包目录，增量更新，自动保留用户配置
"""

import os
import json
import hashlib
import shutil
import tempfile
import threading
import time
import zipfile
import concurrent.futures
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Callable

# ==================== 压缩包 / Modrinth(mrpack) 支持 ====================

# 最终“整合包根目录”缓存：{(压缩包路径, 修改时间, 大小): 目录}
# 同一个压缩包只处理一次，检测 / 更新 / 回退过程中重复使用
_ZIP_EXTRACT_CACHE: Dict[tuple, Path] = {}

# Modrinth 清单元信息缓存：{同上 key: {name, version_id, minecraft, loaders}}
_MRPACK_META_CACHE: Dict[tuple, dict] = {}

# 本程序创建的所有临时目录（退出时统一清理）
_TEMP_DIRS: List[Path] = []

# 临时目录的“容器”目录（如 D:\.mc_updater_tmp），退出时若为空一并删除
_TEMP_PARENTS: set = set()

# Modrinth 整合包清单文件名
MRPACK_MANIFEST_NAME = "modrinth.index.json"

# 支持直接选择的压缩包后缀
ARCHIVE_SUFFIXES = (".zip", ".mrpack")

# Modrinth 资源下载缓存目录（按文件 SHA1 命名，可跨次运行复用）
_MRPACK_CACHE_DIR: Optional[Path] = None

# 并发下载线程数
_DOWNLOAD_WORKERS = 8


def cleanup_zip_cache():
    """删除所有压缩包解压 / 下载产生的临时目录（程序退出时调用）"""
    for path in list(_TEMP_DIRS):
        try:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass
    _TEMP_DIRS.clear()
    # 顺带清掉空的容器目录（D:\.mc_updater_tmp 之类）
    for parent in list(_TEMP_PARENTS):
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass
    _TEMP_PARENTS.clear()
    _ZIP_EXTRACT_CACHE.clear()
    _MRPACK_META_CACHE.clear()


def _cache_key(zip_path: Path) -> tuple:
    try:
        stat = zip_path.stat()
        return (str(zip_path), int(stat.st_mtime), stat.st_size)
    except OSError:
        return (str(zip_path), 0, 0)


def _new_temp_dir(prefix: str, base: Optional[Path] = None) -> Path:
    """
    创建并登记一个临时目录。
    base 给定时，优先创建在 base 所在盘符的「.updater/_tmp」下，
    避免大整合包解压/下载把系统盘（通常是 C:）撑爆；
    base 不存在时退到该盘根目录的 .mc_updater_tmp；再失败则用系统临时目录。
    """
    root: Optional[Path] = None
    if base is not None:
        try:
            b = Path(base)
            if b.is_file():
                b = b.parent
            if b.exists():
                # 放在更新器自己的数据目录里：同盘、且已被扫描忽略
                cand = b / ".updater" / "_tmp"
                cand.mkdir(parents=True, exist_ok=True)
                _TEMP_PARENTS.add(cand)
                root = cand
            else:
                anchor = b.anchor or ""
                if anchor:
                    cand = Path(anchor) / ".mc_updater_tmp"
                    cand.mkdir(parents=True, exist_ok=True)
                    _TEMP_PARENTS.add(cand)
                    root = cand
        except OSError:
            root = None
    try:
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(root) if root else None))
    except OSError:
        path = Path(tempfile.mkdtemp(prefix=prefix))
    _TEMP_DIRS.append(path)
    return path


def _version_tuple(text) -> tuple:
    """把版本字符串转成可比较的数字元组，如 '21.1.250' -> (21, 1, 250)"""
    import re
    parts = re.findall(r'\d+', str(text or ""))
    return tuple(int(p) for p in parts[:3]) if parts else (0,)


def _file_sha1(path: Path) -> Optional[str]:
    """计算文件 SHA1（Modrinth 清单使用的就是 sha1）"""
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 256)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _link_or_copy(src: Path, dst: Path):
    """优先硬链接（同盘符零额外占用），失败则复制"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        try:
            dst.unlink()
        except OSError:
            pass
    try:
        os.link(str(src), str(dst))
        return
    except OSError:
        pass
    shutil.copy2(src, dst)


def _get_mrpack_cache_dir() -> Path:
    """Modrinth 下载缓存目录（放在系统临时目录，可跨次运行复用）"""
    global _MRPACK_CACHE_DIR
    if _MRPACK_CACHE_DIR is None or not _MRPACK_CACHE_DIR.exists():
        d = Path(tempfile.gettempdir()) / "mc_updater_downloads"
        d.mkdir(parents=True, exist_ok=True)
        _MRPACK_CACHE_DIR = d
    return _MRPACK_CACHE_DIR


def clear_mrpack_cache() -> int:
    """清空 Modrinth 下载缓存，返回释放的字节数"""
    cache_dir = _get_mrpack_cache_dir()
    freed = 0
    try:
        for f in cache_dir.iterdir():
            try:
                if f.is_file():
                    freed += f.stat().st_size
                    f.unlink()
            except OSError:
                continue
    except OSError:
        pass
    return freed


def _enforce_mrpack_cache_limit(max_bytes: int = 2 * 1024 * 1024 * 1024):
    """把下载缓存控制在 max_bytes 以内，超出时按最久未使用顺序清理"""
    cache_dir = _get_mrpack_cache_dir()
    try:
        entries = []
        total = 0
        for f in cache_dir.iterdir():
            try:
                st = f.stat()
            except OSError:
                continue
            if not f.is_file():
                continue
            entries.append((st.st_mtime, st.st_size, f))
            total += st.st_size
    except OSError:
        return

    if total <= max_bytes:
        return

    entries.sort()  # 最旧的先删
    for _mtime, size, path in entries:
        if total <= max_bytes:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            continue


def _http_download(url: str, dest: Path, expected_sha1: str = "", timeout: int = 90) -> int:
    """
    流式下载 url 到 dest（先写 .part，校验通过后再改名），返回字节数。
    校验失败会抛 ValueError。
    """
    import ssl
    import urllib.request

    # 安全：只允许 http/https，防止清单里塞 file:// 之类的本地协议被读取
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme not in ("http", "https"):
        raise ValueError(f"不支持的下载协议（仅允许 http/https）：{url[:80]}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = Path(str(dest) + ".part")
    req = urllib.request.Request(
        url, headers={"User-Agent": "MCModpackUpdater/1.0 (+modpack incremental updater)"}
    )

    def _open(ctx=None):
        if ctx is None:
            return urllib.request.urlopen(req, timeout=timeout)
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)

    try:
        resp = _open()
    except ssl.SSLError:
        # 少数系统缺少根证书，降级重试
        resp = _open(ssl._create_unverified_context())

    total = 0
    try:
        with resp, open(part, "wb") as f:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)

        if expected_sha1:
            actual = _file_sha1(part)
            if actual != expected_sha1:
                raise ValueError(f"哈希校验失败（期望 {expected_sha1[:8]}…，实际 {str(actual)[:8]}…）")

        os.replace(str(part), str(dest))
    except BaseException:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        raise

    return total


def _find_mrpack_index(root: Path) -> Optional[Path]:
    """在解压根目录（或下一层）寻找 modrinth.index.json"""
    direct = root / MRPACK_MANIFEST_NAME
    if direct.is_file():
        return direct
    try:
        for child in root.iterdir():
            if child.is_dir():
                candidate = child / MRPACK_MANIFEST_NAME
                if candidate.is_file():
                    return candidate
    except OSError:
        pass
    return None


def _download_mrpack_files(files: List[dict], target_root: Path,
                           log_callback=None, progress_callback=None) -> Dict:
    """
    按 Modrinth 清单并发下载所有声明文件到 target_root。
    :return: {"ok": [...], "failed": [...], "skipped": [...], "missing": [...]}
    """
    log = log_callback or (lambda *a: None)
    progress = progress_callback or (lambda *a: None)
    cache_dir = _get_mrpack_cache_dir()

    todo: List[dict] = []
    skipped: List[str] = []
    for item in files or []:
        rel = str(item.get("path") or "").replace("\\", "/")
        env = item.get("env") or {}
        if str(env.get("client", "")).lower() == "unsupported":
            skipped.append(rel)
            continue
        if not rel:
            continue
        todo.append(item)

    total = len(todo)
    if total == 0:
        return {"ok": [], "failed": [], "skipped": skipped, "missing": [], "reasons": {}}

    total_bytes = sum(int(i.get("fileSize") or 0) for i in todo)
    log(f"开始下载清单资源：{total} 个文件，约 {format_size(total_bytes)}", "info")
    log(f"下载缓存目录：{cache_dir}（缓存上限 2GB，超出会自动清理最旧的）", "info")
    _enforce_mrpack_cache_limit()

    done_files = 0
    done_bytes = 0
    ok: List[str] = []
    failed: List[str] = []
    reasons: Dict[str, str] = {}
    lock = threading.Lock()

    def one(item: dict):
        rel = str(item.get("path") or "").replace("\\", "/")
        dest = target_root / rel
        sha1 = str((item.get("hashes") or {}).get("sha1") or "").lower()
        size = int(item.get("fileSize") or 0)

        # 1) 目标已存在且校验通过 → 跳过
        if dest.is_file() and sha1 and _file_sha1(dest) == sha1:
            return True, rel, size, ""

        # 2) 命中本地下载缓存
        cache_file = (cache_dir / sha1) if sha1 else None
        if cache_file is not None and cache_file.is_file():
            if (not sha1) or _file_sha1(cache_file) == sha1:
                try:
                    _link_or_copy(cache_file, dest)
                    return True, rel, size, ""
                except OSError:
                    pass

        urls = [u for u in (item.get("downloads") or []) if u]
        if not urls:
            return False, rel, size, "清单未提供下载地址"

        last_err = ""
        for attempt in range(3):
            for url in urls:
                try:
                    _http_download(url, dest, sha1, timeout=90)
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
                    # 限流 / 服务端临时错误 → 退避后重试
                    if any(code in last_err for code in ("429", "500", "502", "503", "504")):
                        time.sleep(1.5 * (attempt + 1))
                    continue
                # 回写缓存
                if cache_file is not None:
                    try:
                        if not (cache_file.is_file() and _file_sha1(cache_file) == sha1):
                            shutil.copy2(dest, cache_file)
                    except OSError:
                        pass
                return True, rel, size, ""
        return False, rel, size, last_err or "下载失败"

    with concurrent.futures.ThreadPoolExecutor(max_workers=_DOWNLOAD_WORKERS) as pool:
        future_map = {pool.submit(one, item): item for item in todo}
        for fut in concurrent.futures.as_completed(future_map):
            item = future_map[fut]
            rel = str(item.get("path") or "").replace("\\", "/")
            size = int(item.get("fileSize") or 0)
            reason = ""
            try:
                success, rel, size, reason = fut.result()
            except Exception as e:
                log(f"下载异常 {rel}: {e}", "warning")
                success = False
                reason = f"{type(e).__name__}: {e}"

            with lock:
                done_files += 1
                done_bytes += size
                if success:
                    ok.append(rel)
                else:
                    failed.append(rel)
                    reasons[rel] = reason
                    log(f"下载失败：{rel}（{reason}）", "warning")

                if total_bytes > 0:
                    progress(done_bytes, total_bytes,
                             f"下载整合包资源 {done_files}/{total}：{Path(rel).name}")
                else:
                    progress(done_files, total,
                             f"下载整合包资源 {done_files}/{total}：{Path(rel).name}")

    level = "info" if not failed else "warning"
    log(f"资源下载完成：成功 {len(ok)} 个，失败 {len(failed)} 个", level)

    # 校验清单里声明但最终缺失的文件（通常是清单自带、或下载失败）
    missing = []
    for item in todo:
        rel = str(item.get("path") or "").replace("\\", "/")
        if not (target_root / rel).is_file():
            missing.append(rel)

    return {"ok": ok, "failed": failed, "skipped": skipped,
            "missing": missing, "reasons": reasons}


def _assemble_mrpack(raw_root: Path, index_file: Path,
                     log_callback=None, progress_callback=None,
                     base_hint: Optional[Path] = None) -> Tuple[Path, Dict]:
    """
    把 Modrinth 整合包组装成一个完整的整合包根目录：
      overrides/ + client-overrides/ 内容 + 清单里声明的全部资源（联网下载）
    :param base_hint: 整合包所在目录，用于把临时文件放到同一个盘符（省系统盘空间）
    :return: (整合包根目录, 清单元信息)
    """
    log = log_callback or (lambda *a: None)
    progress = progress_callback or (lambda *a: None)

    try:
        data = json.loads(index_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"Modrinth 整合包清单解析失败：{e}")

    pack_name = data.get("name") or "Modrinth 整合包"
    pack_ver = data.get("versionId") or data.get("version") or ""
    deps = data.get("dependencies") or {}
    game_ver = deps.get("minecraft", "")
    loaders = {}
    for key in ("neoforge", "forge", "fabric-loader", "quilt-loader"):
        if deps.get(key):
            loaders[key] = deps[key]
    loader_text = "、".join(f"{k} {v}" for k, v in loaders.items())

    log(f"检测到 Modrinth 整合包：{pack_name} {pack_ver}"
        f"（MC {game_ver} {loader_text}）", "info")

    merged = _new_temp_dir("mc_mrpack_", base=base_hint)

    # 1) overrides 内容（config / kubejs / 本地自有 mod 等）
    #    client-overrides 按 Modrinth 规范是"仅客户端"覆盖层，后应用、优先级更高
    for folder, label in (("overrides", "overrides"), ("client-overrides", "client-overrides")):
        src_dir = raw_root / folder
        if not src_dir.is_dir():
            continue
        moved = 0
        for item in src_dir.iterdir():
            dst = merged / item.name
            try:
                if dst.exists():
                    # 同名（一般是目录）：合并进去，保证后者的文件覆盖前者
                    if item.is_dir() and dst.is_dir():
                        for sub in item.rglob("*"):
                            target = dst / sub.relative_to(item)
                            if sub.is_dir():
                                target.mkdir(parents=True, exist_ok=True)
                            else:
                                target.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(sub, target)
                    else:
                        if dst.is_dir():
                            shutil.rmtree(dst, ignore_errors=True)
                        elif dst.is_file():
                            dst.unlink()
                        shutil.move(str(item), str(dst))
                else:
                    shutil.move(str(item), str(dst))
                moved += 1
            except (OSError, shutil.Error) as e:
                log(f"合并 {label} 失败 {item.name}: {e}", "warning")
        log(f"已合并 {label} 内容：{moved} 项", "info")

    # 2) 清单声明的资源（需要联网下载）
    files = data.get("files") or []
    if files:
        # 磁盘空间预检查：避免下载到一半才发现空间不足
        need = sum(int(f.get("fileSize") or 0) for f in files)
        try:
            free = shutil.disk_usage(str(merged)).free
            if need and free < need * 1.3:
                raise ValueError(
                    f"临时盘空间不足：本次需要约 {format_size(int(need * 1.3))}，"
                    f"当前可用 {format_size(free)}。请清理磁盘后重试。"
                )
        except OSError:
            pass

        result = _download_mrpack_files(files, merged, log, progress)
        if result.get("missing"):
            log(f"注意：有 {len(result['missing'])} 个清单文件最终缺失，"
                f"可能导致游戏启动失败（可重试检测以重新下载）", "warning")
        if result.get("failed"):
            first = result["failed"][0]
            why = (result.get("reasons") or {}).get(first, "未知原因")
            raise ValueError(
                f"有 {len(result['failed'])} 个资源下载失败，请检查网络后重试。\n"
                f"首个失败：{first}\n失败原因：{why}"
            )
    else:
        log("清单中未声明需下载的文件", "info")

    meta = {
        "name": pack_name,
        "version_id": pack_ver,
        "minecraft": game_ver,
        "loaders": loaders,
        "file_count": len(files),
    }
    return merged, meta


def _prepare_new_pack(archive: Path, log_callback=None,
                      progress_callback=None,
                      base_hint: Optional[Path] = None) -> Tuple[Path, Optional[Dict]]:
    """
    把“新版本压缩包”处理成可直接对比的整合包根目录：
      - 普通 zip：解压后交给 _resolve_pack_root 继续识别（含套壳 / overrides）
      - Modrinth 整合包（.mrpack 或含 modrinth.index.json 的 zip）：
        自动下载清单里声明的全部模组，再与 overrides 合并
    同一个压缩包只处理一次。
    :param base_hint: 整合包所在目录，临时文件优先放到同一盘符
    :return: (整合包根目录, Modrinth 元信息或 None)
    """
    log = log_callback or (lambda *a: None)
    progress = progress_callback or (lambda *a: None)

    key = _cache_key(archive)
    cached = _ZIP_EXTRACT_CACHE.get(key)
    if cached and cached.exists():
        return cached, _MRPACK_META_CACHE.get(key)

    temp_root = _new_temp_dir("mc_pack_", base=base_hint)
    total = 0
    log(f"正在解压新版本压缩包：{archive.name}", "info")
    try:
        with zipfile.ZipFile(archive) as zf:
            members = zf.infolist()
            total = len(members)
            for i, member in enumerate(members, 1):
                # 防止压缩包内的路径穿越
                name = member.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    continue
                zf.extract(member, temp_root)
                if total <= 20 or i % 20 == 0 or i == total:
                    progress(i, total, f"解压：{name}")
    except (zipfile.BadZipFile, OSError) as e:
        shutil.rmtree(temp_root, ignore_errors=True)
        log(f"解压失败：{e}", "error")
        raise ValueError(f"压缩包解压失败：{e}")

    meta: Optional[Dict] = None
    index_file = _find_mrpack_index(temp_root)
    if index_file is not None:
        index_dir = index_file.parent
        final_root, meta = _assemble_mrpack(index_dir, index_file, log, progress,
                                            base_hint=base_hint)
        _MRPACK_META_CACHE[key] = meta
        # 原始解压目录已经没用了（overrides 已移走）
        try:
            shutil.rmtree(temp_root, ignore_errors=True)
            if temp_root in _TEMP_DIRS:
                _TEMP_DIRS.remove(temp_root)
        except OSError:
            pass
    else:
        log(f"解压完成，共 {total} 个文件", "info")
        final_root = temp_root

    _ZIP_EXTRACT_CACHE[key] = final_root
    return final_root, meta


def _extract_zip_to_temp(zip_path: Path, log_callback=None, progress_callback=None) -> Path:
    """兼容旧调用：把压缩包处理成整合包根目录"""
    return _prepare_new_pack(zip_path, log_callback, progress_callback)[0]



class SimpleUpdater:
    """简化版整合包更新器"""

    # 备份保留份数：更新成功后只保留最近 N 份，避免备份无限堆积占满磁盘
    BACKUP_KEEP = 3

    # 硬保留：用户数据，永远保留，不参与更新/删除
    HARD_PRESERVE_PATTERNS = [
        "saves/",            # 存档（绝对不能动）
        "servers.dat",       # 服务器列表
        "resourcepacks/",    # 资源包
        "shaderpacks/",      # 光影包
        "schematics/",       # 原理图
        "replay_recordings/", # 回放录像
        "screenshots/",      # 截图
        "mods/*.disabled",   # 被禁用的模组
    ]

    # 可选保留：配置文件，用户可选择是否保留
    # 默认不保留（跟随整合包更新）
    CONFIG_PRESERVE_PATTERNS = [
        "config/",           # 模组配置
        "options.txt",       # 游戏选项（键位等）
        "optionsshaders.txt", # 光影设置
    ]

    # 默认忽略的文件/目录（不参与更新）
    DEFAULT_IGNORE_PATTERNS = [
        ".updater/",         # 更新器自身数据
        ".mixin.out/",
        "logs/",             # 日志
        "crash-reports/",    # 崩溃报告
        "versions/",         # 游戏版本目录
        "libraries/",        # 游戏库
        "assets/",           # 游戏资源
        "natives/",          # 原生库
        "runtime/",          # 运行时
        "*.log",
        "*.log.gz",
        "*.tmp",
        "*.part",            # 下载中断残留的临时文件
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
        # 整合包包装文件（CurseForge / 启动器导出，只在根目录层级生效）
        "manifest.json",
        "modlist.html",
        "minecraftinstance.json",
        ".curseclient",
        "instance.cfg",
        "mmc-pack.json",
        "cover.jpg",
        "icon.png",
    ]

    # 判断“这是一个整合包目录”的标志性内容目录
    PACK_CONTENT_DIRS = ["mods", "kubejs", "config", "defaultconfigs"]

    # 整合包“包装文件”特征（出现这些说明外面还套了一层壳）
    PACK_WRAPPER_MARKERS = [
        "manifest.json",
        "modlist.html",
        "minecraftinstance.json",
        ".curseclient",
    ]

    @classmethod
    def _resolve_pack_root(cls, directory: Path) -> Tuple[Path, str]:
        """
        自动定位整合包的真实根目录，避免因为目录结构不同而误删/误更新文件。
        依次识别（可连续下钻）：
          1. 目录本身就是整合包根目录（有 mods/ 或 kubejs/）
          2. 选中的是 .minecraft 根目录（PCL/HMCL 版本隔离）→ versions/<实例>/
          3. 解压出来的 CurseForge 整合包（带 overrides/ 外壳）→ overrides/
          4. 解压后只有一层同名文件夹 → 自动进入
        返回 (真实根目录, 说明文字)
        """
        notes: List[str] = []
        current = directory

        for _ in range(4):  # 最多下钻 4 层，避免死循环
            try:
                if not current.exists() or not current.is_dir():
                    break
            except OSError:
                break

            # 1) 本身就是整合包根目录
            if any((current / d).exists() for d in ("mods", "kubejs")):
                break

            # 2) .minecraft 根目录 → 版本隔离的实例目录
            versions = current / "versions"
            if versions.is_dir():
                try:
                    candidates = [
                        child for child in versions.iterdir()
                        if child.is_dir()
                        and any((child / d).exists() for d in cls.PACK_CONTENT_DIRS)
                    ]
                except OSError:
                    candidates = []
                if candidates:
                    chosen = max(candidates, key=lambda p: p.stat().st_mtime)
                    if len(candidates) > 1:
                        notes.append(
                            f"检测到多个实例目录，已自动使用最近修改的：{chosen}"
                            f"（如不对请手动选择实例文件夹）"
                        )
                    else:
                        notes.append(f"检测到启动器实例目录，已自动使用：{chosen}")
                    current = chosen
                    continue

            # 3) CurseForge overrides 外壳
            overrides = current / "overrides"
            if overrides.is_dir() and any(
                (overrides / d).exists() for d in cls.PACK_CONTENT_DIRS
            ):
                notes.append(f"检测到 CurseForge overrides 结构，已自动使用：{overrides}")
                current = overrides
                continue

            # 4) 解压后的单层包装目录
            try:
                children = [c for c in current.iterdir() if c.is_dir()]
                root_files = [f for f in current.iterdir() if f.is_file()]
            except OSError:
                break
            if len(children) == 1 and not root_files:
                child = children[0]
                if (child / "overrides").is_dir() or any(
                    (child / d).exists() for d in cls.PACK_CONTENT_DIRS
                ):
                    notes.append(f"检测到单层包装目录，已自动进入：{child}")
                    current = child
                    continue

            break

        return current, "；".join(notes)

    def __init__(self, old_dir: str, new_dir: str,
                 progress_callback: Optional[Callable] = None,
                 log_callback: Optional[Callable] = None,
                 preserve_config: bool = False,
                 extra_preserve_patterns: Optional[List[str]] = None,
                 ignore_patterns: Optional[List[str]] = None,
                 delete_removed: bool = True):
        """
        初始化更新器
        :param old_dir: 本地旧整合包目录
        :param new_dir: 新整合包目录（要更新到的版本）
        :param progress_callback: 进度回调 (current, total, message)
        :param log_callback: 日志回调 (message, level)
        :param preserve_config: 是否保留配置文件（config/、options.txt 等）
        :param extra_preserve_patterns: 额外的保留文件模式
        :param ignore_patterns: 额外的忽略模式
        :param delete_removed: 是否删除新版本中没有的旧文件
        """
        self.progress_callback = progress_callback or (lambda *a: None)
        self.log_callback = log_callback or (lambda *a: None)

        # 最近一次 compare() 扫描出来的旧目录清单（供备份阶段复用，避免重复全盘哈希）
        self._last_old_scan: Optional[Dict[str, dict]] = None

        # 先定位旧整合包根目录：新版本压缩包解压/下载的临时目录会放到同一个盘符，
        # 避免大整合包把系统盘（通常是 C:）撑爆
        self.old_dir, self._old_root_note = self._resolve_pack_root(Path(old_dir).resolve())

        # 清掉上次异常退出（崩溃 / 强杀）残留的临时文件，避免一直占着磁盘
        self._clear_stale_temp()

        # 新版本支持直接选择压缩包：
        #   - .zip    普通整合包，自动解压
        #   - .mrpack Modrinth 整合包，自动下载清单里声明的模组
        self.mrpack_meta: Optional[dict] = None
        new_path = Path(new_dir).resolve()
        if new_path.is_file() and new_path.suffix.lower() in ARCHIVE_SUFFIXES:
            new_path, self.mrpack_meta = _prepare_new_pack(
                new_path, self.log_callback, self.progress_callback,
                base_hint=self.old_dir
            )

        # 自动识别整合包真实根目录（CurseForge overrides 外壳 / 启动器版本隔离）
        self.new_dir, self._new_root_note = self._resolve_pack_root(new_path)

        self.preserve_config = preserve_config
        self.ignore_patterns = list(set(self.DEFAULT_IGNORE_PATTERNS + (ignore_patterns or [])))
        self.delete_removed = delete_removed

        # 组装完整保留列表：硬保留 + 可选配置保留 + 额外保留
        all_preserve = list(self.HARD_PRESERVE_PATTERNS)
        if preserve_config:
            all_preserve += self.CONFIG_PRESERVE_PATTERNS
        if extra_preserve_patterns:
            all_preserve += extra_preserve_patterns
        self.preserve_patterns = list(set(all_preserve))

        self.updater_dir = self.old_dir / ".updater"
        self.backup_dir = self.updater_dir / "backup"
        self.manifest_path = self.updater_dir / "manifest.json"

        self._ensure_dirs()

        # 提示自动识别到的真实目录，方便用户确认
        if self._old_root_note:
            self._log(f"旧整合包：{self._old_root_note}", "info")
        if self._new_root_note:
            self._log(f"新整合包：{self._new_root_note}", "info")

    def _clear_stale_temp(self):
        """
        清掉上次异常退出残留的临时目录（整合包目录下的 .updater/_tmp）。
        正常关闭程序时会删除，但崩溃 / 强制结束进程时可能留下来占空间。

        注意：只删「本进程没有在用」的目录。同一个进程里可能同时存在多个
        更新器实例（比如检测用的实例 + 回退对话框的实例），不能误删别人正在用的。
        """
        stale = self.old_dir / ".updater" / "_tmp"
        try:
            if not stale.is_dir():
                return
            in_use = set()
            for p in list(_TEMP_DIRS):
                try:
                    in_use.add(p.resolve())
                except OSError:
                    continue
            for child in stale.iterdir():
                try:
                    if child.resolve() in in_use:
                        continue
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink()
                except OSError:
                    continue
        except OSError:
            pass

    def _ensure_dirs(self):
        """确保必要目录存在"""
        self.updater_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _log(self, message: str, level: str = "info"):
        self.log_callback(message, level)

    def _progress(self, current: int, total: int, message: str = ""):
        self.progress_callback(current, total, message)

    @staticmethod
    def file_sha256(filepath: Path) -> str:
        """计算文件 SHA256"""
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _should_ignore(self, rel_path: str) -> bool:
        """检查路径是否应该被忽略"""
        import fnmatch
        rel_path = rel_path.replace("\\", "/")
        for pattern in self.ignore_patterns:
            if pattern.endswith("/"):
                if rel_path.startswith(pattern) or rel_path + "/" == pattern:
                    return True
            elif "*" in pattern:
                if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(Path(rel_path).name, pattern):
                    return True
            else:
                if rel_path == pattern:
                    return True
        return False

    def _match_preserve(self, rel_path: str) -> bool:
        """检查路径是否匹配保留模式"""
        import fnmatch
        rel_path = rel_path.replace("\\", "/")
        for pattern in self.preserve_patterns:
            if pattern.endswith("/"):
                if rel_path.startswith(pattern) or rel_path + "/" == pattern:
                    return True
            elif "*" in pattern:
                if fnmatch.fnmatch(rel_path, pattern):
                    return True
            else:
                if rel_path == pattern:
                    return True
        return False

    def scan_directory(self, directory: Path, label: str = "") -> Dict[str, dict]:
        """
        扫描目录，返回文件清单 {相对路径: {sha256, size}}
        :param label: 非空时会把校验进度回调出去（大整合包不至于像卡死）
        """
        result = {}
        if not directory.exists():
            return result

        # 先收集要处理的文件列表（顺便知道总数，用于显示进度）
        targets: List[Tuple[str, Path]] = []
        for root, dirs, files in os.walk(directory):
            root_path = Path(root)
            rel_root = root_path.relative_to(directory)
            rel_root_str = str(rel_root).replace("\\", "/")
            if rel_root_str == ".":
                rel_root_str = ""

            # 过滤忽略的目录
            dirs[:] = [d for d in dirs if not self._should_ignore(
                (rel_root_str + "/" + d + "/").lstrip("/")
            )]

            for file in files:
                rel_path = (rel_root_str + "/" + file).lstrip("/")
                if self._should_ignore(rel_path):
                    continue
                targets.append((rel_path, root_path / file))

        total = len(targets)
        if label:
            self._log(f"正在校验{label}：共 {total} 个文件", "info")

        for index, (rel_path, file_path) in enumerate(targets, 1):
            try:
                file_hash = self.file_sha256(file_path)
                file_size = file_path.stat().st_size
                result[rel_path] = {
                    "sha256": file_hash,
                    "size": file_size
                }
            except IOError as e:
                self._log(f"读取文件失败 {rel_path}: {e}", "warning")

            # 大整合包：每隔一定数量回报一次进度，避免界面看起来卡死
            if label and total > 50 and (index % 50 == 0 or index == total):
                self._progress(index, total, f"校验{label} {index}/{total}")

        return result

    def load_manifest(self) -> Optional[dict]:
        """加载本地基准清单（记录上次更新时的文件状态）"""
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return None
        return None

    def save_manifest(self, files: dict, version_label: str = ""):
        """保存基准清单"""
        manifest = {
            "version_label": version_label,
            "update_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "files": files
        }
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

    def get_user_modified_files(self, scanned: Optional[Dict[str, dict]] = None) -> List[str]:
        """
        获取用户修改过的文件列表（对比当前文件和基准清单）
        :param scanned: 已经扫描好的旧目录清单（复用可省一次全盘哈希，大整合包很关键）
        """
        manifest = self.load_manifest()
        if not manifest:
            # 没有基准清单，认为所有保留模式的文件都是用户修改的
            return []

        baseline = manifest.get("files", {})
        modified = []

        for rel_path in baseline.keys():
            if not self._match_preserve(rel_path):
                continue
            local_file = self.old_dir / rel_path
            if not local_file.exists():
                continue
            try:
                actual_hash = self.file_sha256(local_file)
                if actual_hash != baseline[rel_path].get("sha256"):
                    modified.append(rel_path)
            except IOError:
                modified.append(rel_path)

        # 还有基准清单中没有但存在的保留文件（用户自己加的）
        current = scanned if scanned is not None else self.scan_directory(self.old_dir)
        for rel_path in current.keys():
            if self._match_preserve(rel_path) and rel_path not in baseline:
                modified.append(rel_path)

        return modified

    def detect_neoforge_version(self, pack_dir: Path = None) -> Optional[str]:
        """
        检测整合包中安装的 NeoForge / Forge 版本。
        优先从 mods/ 目录下的 neoforge/forge jar 文件名提取，其次检查 versions/ 目录。
        返回版本号字符串，如 "21.1.250"，检测不到返回 None。
        """
        import re
        pack_dir = pack_dir or self.old_dir
        pack_dir = Path(pack_dir)

        # 候选位置
        candidates = [
            pack_dir / "mods",
            pack_dir / "libraries" / "net" / "neoforged" / "neoforge",
            pack_dir / "libraries" / "net" / "minecraftforge" / "forge",
        ]

        # 检查 versions 目录（启动器实例格式）
        versions_dir = pack_dir / "versions"
        if versions_dir.exists():
            for v in versions_dir.iterdir():
                if v.is_dir():
                    candidates.append(v / "mods")

        version = None
        for mods_dir in candidates:
            if not mods_dir.exists():
                continue
            try:
                for f in mods_dir.iterdir():
                    name = f.name.lower()
                    if f.is_dir():
                        # 可能是版本目录，比如 neoforge/21.1.250/
                        if "neoforge" in name or "forge" in name:
                            # 目录名本身可能就是版本号
                            ver_match = re.search(r'(\d+\.\d+\.\d+)', f.name)
                            if ver_match:
                                return ver_match.group(1)
                            # 或者里面有 jar
                            for sub in f.iterdir():
                                if sub.suffix.lower() in (".jar",):
                                    m = re.search(r'(?:neoforge|forge)[-_](\d+\.\d+[\.\d]*)', sub.name.lower())
                                    if m:
                                        return m.group(1)
                        continue
                    if f.suffix.lower() not in (".jar",):
                        continue
                    # 匹配 neoforge-21.1.250.jar / forge-1.20.1-47.2.0.jar
                    m = re.search(r'(?:neoforge|forge)[-_](\d+\.\d+[\.\d]*)', name)
                    if m:
                        version = m.group(1)
                        # 优先选 NeoForge（数字更大的通常是 NeoForge）
                        if "neoforge" in name:
                            return version
            except OSError:
                continue

        # 启动器实例目录（PCL/HMCL 版本隔离）：<实例名>.json 里记录了加载器版本
        try:
            for js in sorted(pack_dir.glob("*.json")):
                try:
                    text = js.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for pattern in (
                    r'neoforged:neoforge:(\d+\.\d+[\.\d]*)',
                    r'neoforge[-_](\d+\.\d+[\.\d]*)',
                ):
                    m = re.search(pattern, text)
                    if m:
                        return m.group(1)
                if not version:
                    m = re.search(r'minecraftforge:forge:(\d+\.\d+[\.\d]*)', text)
                    if m:
                        version = m.group(1)
        except OSError:
            pass

        return version

    def detect_fabric_version(self, pack_dir: Path = None) -> Optional[str]:
        """
        检测整合包中安装的 Fabric Loader 版本。
        从 mods/ 目录下的 fabric-loader jar 文件名提取。
        返回版本号字符串，检测不到返回 None。
        """
        import re
        pack_dir = pack_dir or self.old_dir
        pack_dir = Path(pack_dir)

        candidates = [pack_dir / "mods"]

        # 检查 versions 目录（启动器实例格式）
        versions_dir = pack_dir / "versions"
        if versions_dir.exists():
            for v in versions_dir.iterdir():
                if v.is_dir():
                    candidates.append(v / "mods")

        for mods_dir in candidates:
            if not mods_dir.exists():
                continue
            try:
                for f in mods_dir.iterdir():
                    if f.suffix.lower() not in (".jar",):
                        continue
                    name = f.name.lower()
                    # 匹配 fabric-loader-0.15.11.jar 等
                    m = re.search(r'fabric[-_]loader[-_](\d+[\.\d]+)', name)
                    if m:
                        return m.group(1)
            except OSError:
                continue

        # 启动器实例目录：<实例名>.json 里记录了 fabric-loader 版本
        try:
            for js in sorted(pack_dir.glob("*.json")):
                try:
                    text = js.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                m = re.search(r'fabricmc:fabric-loader:(\d+[\.\d]+)', text)
                if m:
                    return m.group(1)
        except OSError:
            pass

        return None

    def detect_required_fabric(self, pack_dir: Path = None) -> Dict:
        """
        检测新版本模组要求的最低 Fabric Loader 版本。
        从模组 jar 的 fabric.mod.json 中读取 depends.fabricloader 约束。
        返回 {version: 最高版本号, demanding_mods: [(模组名, 要求版本)]}
        """
        import re
        import zipfile
        import json

        pack_dir = pack_dir or self.new_dir
        pack_dir = Path(pack_dir)

        mods_dir = pack_dir / "mods"
        if not mods_dir.exists():
            return {"version": None, "demanding_mods": []}

        max_version = None
        demanding_mods = []

        def _parse_version(ver_str: str) -> Optional[tuple]:
            try:
                parts = re.findall(r'\d+', ver_str)
                return tuple(int(p) for p in parts[:3]) if parts else None
            except ValueError:
                return None

        def _compare_ver(v1: str, v2: str) -> int:
            t1 = _parse_version(v1)
            t2 = _parse_version(v2)
            if not t1 and not t2:
                return 0
            if not t1:
                return -1
            if not t2:
                return 1
            max_len = max(len(t1), len(t2))
            t1 = t1 + (0,) * (max_len - len(t1))
            t2 = t2 + (0,) * (max_len - len(t2))
            if t1 > t2:
                return 1
            elif t1 < t2:
                return -1
            return 0

        try:
            for jar_file in mods_dir.iterdir():
                if jar_file.suffix.lower() != ".jar" or not jar_file.is_file():
                    continue
                try:
                    with zipfile.ZipFile(jar_file) as zf:
                        try:
                            json_data = zf.read("fabric.mod.json").decode("utf-8", errors="ignore")
                            mod_info = json.loads(json_data)
                        except (KeyError, json.JSONDecodeError):
                            continue

                        mod_name = mod_info.get("name") or mod_info.get("id") or jar_file.stem
                        depends = mod_info.get("depends", {})
                        fabric_dep = depends.get("fabricloader") or depends.get("fabric-loader", "")

                        if not fabric_dep:
                            continue

                        # 解析版本约束，支持多种格式
                        found_ver = None
                        if isinstance(fabric_dep, str):
                            # 格式如 ">=0.15.0" 或 "*" 或 "0.15.x"
                            ver_match = re.search(r'(\d+[\.\d]+)', fabric_dep)
                            if ver_match:
                                found_ver = ver_match.group(1)
                        elif isinstance(fabric_dep, list):
                            for item in fabric_dep:
                                if isinstance(item, str):
                                    ver_match = re.search(r'(\d+[\.\d]+)', item)
                                    if ver_match:
                                        found_ver = ver_match.group(1)
                                        break

                        if found_ver:
                            if not max_version or _compare_ver(found_ver, max_version) > 0:
                                max_version = found_ver
                            demanding_mods.append((mod_name, found_ver))
                except (zipfile.BadZipFile, OSError):
                    continue
        except OSError:
            pass

        demanding_mods.sort(key=lambda x: _parse_version(x[1]) or (0,), reverse=True)
        demanding_mods = demanding_mods[:10]

        return {"version": max_version, "demanding_mods": demanding_mods}

    @staticmethod
    def is_game_running() -> bool:
        """
        检测 Minecraft 游戏是否正在运行。

        注意：Windows 11 24H2 起已移除 wmic，不能再用它作为唯一手段。
        这里依次使用：tasklist（判断有无 java 进程）
                    → PowerShell CIM / wmic（取命令行判断是不是 Minecraft）
                    → 进程窗口标题兜底。
        全部手段都失败时返回 False（不阻塞用户），避免误报。
        """
        import subprocess

        def _run(args, timeout=12) -> str:
            try:
                r = subprocess.run(
                    args, capture_output=True, text=True, timeout=timeout,
                    encoding="utf-8", errors="ignore",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return (r.stdout or "") + (r.stderr or "")
            except Exception:
                return ""

        keywords = ("minecraft", ".minecraft", "hmcl", "pcl2", "bakaxl",
                    "neoforge", "forge", "fabric")

        # 1) 先看有没有 java 进程（tasklist 所有 Windows 都有）
        proc_list = _run(["tasklist"]).lower()
        if "javaw.exe" not in proc_list and "java.exe" not in proc_list:
            return False

        # 2) 取命令行判断是不是 Minecraft（PowerShell CIM）
        cmdline = _run([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='javaw.exe' or Name='java.exe'\""
            " | Select-Object -ExpandProperty CommandLine"
        ]).lower()
        if cmdline.strip():
            return any(kw in cmdline for kw in keywords)

        # 3) 老系统退路：wmic
        wmic_out = _run([
            "wmic", "process", "where",
            "name='javaw.exe' or name='java.exe'", "get", "commandline"
        ]).lower()
        if wmic_out.strip():
            return any(kw in wmic_out for kw in keywords)

        # 4) 都取不到命令行：用「java 进程有没有可见窗口」兜底
        titles = _run([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Get-Process javaw,java -ErrorAction SilentlyContinue"
            " | Where-Object { $_.MainWindowTitle }"
            " | Select-Object -ExpandProperty MainWindowTitle"
        ])
        return bool(titles.strip())

    def detect_required_neoforge(self, pack_dir: Path = None) -> Dict:
        """
        检测新版本模组要求的最低 NeoForge 版本。
        从模组 jar 的 META-INF/neoforge.mods.toml 或 mods.toml 中读取 loader 版本约束。
        返回 {version: 最高版本号, demanding_mods: [要求高版本的模组列表]}
        """
        import re
        import zipfile

        pack_dir = pack_dir or self.new_dir
        pack_dir = Path(pack_dir)

        mods_dir = pack_dir / "mods"
        if not mods_dir.exists():
            return {"version": None, "demanding_mods": []}

        max_version = None
        demanding_mods = []  # [(模组名, 要求版本)]

        def _parse_version(ver_str: str) -> Optional[tuple]:
            """把版本号转成数字元组用于比较"""
            try:
                parts = re.findall(r'\d+', ver_str)
                return tuple(int(p) for p in parts[:3]) if parts else None
            except ValueError:
                return None

        def _compare_ver(v1: str, v2: str) -> int:
            """比较版本号，1=v1大，-1=v2大，0=相等"""
            t1 = _parse_version(v1)
            t2 = _parse_version(v2)
            if not t1 and not t2:
                return 0
            if not t1:
                return -1
            if not t2:
                return 1
            max_len = max(len(t1), len(t2))
            t1 = t1 + (0,) * (max_len - len(t1))
            t2 = t2 + (0,) * (max_len - len(t2))
            if t1 > t2:
                return 1
            elif t1 < t2:
                return -1
            return 0

        def _get_mod_name(toml_data: str, jar_name: str) -> str:
            """从 toml 中提取模组显示名，没有就用文件名"""
            name_match = re.search(r'displayName\s*=\s*"([^"]+)"', toml_data)
            if name_match:
                return name_match.group(1)
            name_match2 = re.search(r'name\s*=\s*"([^"]+)"', toml_data)
            if name_match2:
                return name_match2.group(1)
            return jar_name

        try:
            for jar_file in mods_dir.iterdir():
                if jar_file.suffix.lower() != ".jar" or not jar_file.is_file():
                    continue
                try:
                    with zipfile.ZipFile(jar_file) as zf:
                        toml_data = None
                        for toml_name in ["META-INF/neoforge.mods.toml", "META-INF/mods.toml"]:
                            try:
                                toml_data = zf.read(toml_name).decode("utf-8", errors="ignore")
                                break
                            except KeyError:
                                continue
                        if not toml_data:
                            continue

                        mod_name = _get_mod_name(toml_data, jar_file.stem)
                        loader_matches = re.findall(
                            r'loaderVersion\s*=\s*"([^"]+)"',
                            toml_data
                        )
                        for loader_str in loader_matches:
                            ver_match = re.search(r'[\[\(](\d+\.\d+[\.\d]*)', loader_str)
                            found_ver = None
                            if ver_match:
                                found_ver = ver_match.group(1)
                            else:
                                simple = re.match(r'^(\d+\.\d+[\.\d]*)', loader_str.strip())
                                if simple:
                                    found_ver = simple.group(1)

                            if found_ver:
                                if not max_version or _compare_ver(found_ver, max_version) > 0:
                                    max_version = found_ver
                                # 记录所有有 loader 版本要求的模组
                                demanding_mods.append((mod_name, found_ver))
                except (zipfile.BadZipFile, OSError):
                    continue
        except OSError:
            pass

        # 按版本号从高到低排序，取前10个
        demanding_mods.sort(key=lambda x: _parse_version(x[1]) or (0,), reverse=True)
        demanding_mods = demanding_mods[:10]

        return {"version": max_version, "demanding_mods": demanding_mods}

    def compare(self) -> Dict:
        """
        对比新旧整合包，计算差异
        :return: {added, modified, removed, preserve_count, total_size, old_count, new_count}
        """
        self._log("扫描旧整合包文件...", "info")
        old_files = self.scan_directory(self.old_dir, label="旧整合包")
        self._log(f"旧整合包: {len(old_files)} 个文件", "info")

        self._log("扫描新整合包文件...", "info")
        new_files = self.scan_directory(self.new_dir, label="新整合包")
        self._log(f"新整合包: {len(new_files)} 个文件", "info")

        added = []
        modified = []
        removed = []
        total_size = 0

        for path, info in new_files.items():
            if path not in old_files:
                added.append(path)
                total_size += info.get("size", 0)
            elif old_files[path].get("sha256") != info.get("sha256"):
                modified.append(path)
                total_size += info.get("size", 0)

        # 删除列表：排除保留文件
        for path in old_files:
            if path not in new_files and not self._match_preserve(path):
                removed.append(path)

        # 统计被保留的文件数（旧包里匹配保留模式且在修改列表中的）
        preserve_modified = [
            p for p in modified
            if self._match_preserve(p)
        ]

        # 高风险删除：模组 / 脚本 / 配置文件被大量删除，通常意味着目录选错了
        risky_removed = [
            p for p in removed
            if p.startswith(("mods/", "kubejs/", "config/", "defaultconfigs/"))
        ]
        removed_mods = [p for p in removed if p.startswith("mods/")]
        added_mods = [p for p in added if p.startswith("mods/")]

        # 检测 NeoForge 版本（不阻塞主流程，失败就跳过）
        neoforge_info = {}
        try:
            old_nf = self.detect_neoforge_version(self.old_dir)
            new_nf_result = self.detect_required_neoforge(self.new_dir)
            new_nf_required = new_nf_result.get("version")
            demanding_mods = new_nf_result.get("demanding_mods", [])

            # Modrinth 清单里声明的加载器版本是作者明确写明的，优先采用；
            # 只有当某个模组要求“更高”的版本时，才改用模组的要求
            declared_loaders = (self.mrpack_meta or {}).get("loaders") or {}
            declared_nf = declared_loaders.get("neoforge") or declared_loaders.get("forge")
            declared_nf_source = False
            if declared_nf and (
                not new_nf_required
                or _version_tuple(new_nf_required) <= _version_tuple(declared_nf)
            ):
                new_nf_required = declared_nf
                declared_nf_source = True

            # 兜底：新版本整合包自带的加载器版本（文件夹形式也适用）
            if not new_nf_required:
                new_installed = self.detect_neoforge_version(self.new_dir)
                if new_installed:
                    new_nf_required = new_installed
                    declared_nf = new_installed
                    declared_nf_source = True

            neoforge_info = {
                "old_version": old_nf,
                "new_required": new_nf_required,
                "declared_version": declared_nf,
                "declared_source": declared_nf_source,
                "demanding_mods": demanding_mods,
                "status": "ok",  # ok / too_low / too_high / unknown_old / unknown_new / mismatch
                "message": "",
            }

            def _ver_tuple(v):
                import re
                parts = re.findall(r'\d+', v)
                return tuple(int(p) for p in parts[:3]) if parts else (0,)

            if not old_nf and not new_nf_required:
                neoforge_info["status"] = "unknown"
                neoforge_info["message"] = "未检测到 NeoForge/Forge 版本信息"
            elif not old_nf:
                neoforge_info["status"] = "unknown_old"
                neoforge_info["message"] = f"未能检测到当前安装的加载器版本，模组要求 NeoForge {new_nf_required}+"
            elif not new_nf_required:
                neoforge_info["status"] = "unknown_new"
                neoforge_info["message"] = f"当前安装 NeoForge {old_nf}，未能检测到模组的加载器版本要求"
            else:
                old_t = _ver_tuple(old_nf)
                new_t = _ver_tuple(new_nf_required)
                if old_t < new_t:
                    neoforge_info["status"] = "too_low"
                    neoforge_info["message"] = (
                        f"NeoForge 版本不足！当前 {old_nf}，模组要求 {new_nf_required}+"
                    )
                elif old_t > new_t and len(demanding_mods) > 0:
                    # 当前版本远高于模组要求，可能有兼容问题
                    # 只有当主版本号差距大时才提示（比如 21.x vs 20.x）
                    if old_t[0] > new_t[0]:
                        neoforge_info["status"] = "too_high"
                        neoforge_info["message"] = (
                            f"注意：当前 NeoForge {old_nf} 主版本高于模组要求的 {new_nf_required}，"
                            f"部分模组可能不兼容"
                        )
                    else:
                        neoforge_info["message"] = f"NeoForge 版本兼容（当前 {old_nf}，要求 {new_nf_required}+）"
                else:
                    neoforge_info["message"] = f"NeoForge 版本兼容（当前 {old_nf}，要求 {new_nf_required}+）"
        except Exception as e:
            neoforge_info = {"status": "error", "message": f"版本检测失败: {e}", "demanding_mods": []}
            pass

        # 检测 Fabric 版本（不阻塞主流程，失败就跳过）
        fabric_info = {}
        try:
            old_fabric = self.detect_fabric_version(self.old_dir)
            new_fabric_result = self.detect_required_fabric(self.new_dir)
            new_fabric_required = new_fabric_result.get("version")
            demanding_mods_f = new_fabric_result.get("demanding_mods", [])

            declared_fb = (self.mrpack_meta or {}).get("loaders", {}).get("fabric-loader")
            declared_fb_source = False
            if declared_fb and (
                not new_fabric_required
                or _version_tuple(new_fabric_required) <= _version_tuple(declared_fb)
            ):
                new_fabric_required = declared_fb
                declared_fb_source = True

            # 兜底：新版本整合包自带的 Fabric Loader 版本
            if not new_fabric_required:
                new_fb_installed = self.detect_fabric_version(self.new_dir)
                if new_fb_installed:
                    new_fabric_required = new_fb_installed
                    declared_fb = new_fb_installed
                    declared_fb_source = True

            fabric_info = {
                "old_version": old_fabric,
                "new_required": new_fabric_required,
                "declared_version": declared_fb,
                "declared_source": declared_fb_source,
                "demanding_mods": demanding_mods_f,
                "status": "ok",
                "message": "",
            }

            def _ver_tuple_f(v):
                import re
                parts = re.findall(r'\d+', v)
                return tuple(int(p) for p in parts[:3]) if parts else (0,)

            if not old_fabric and not new_fabric_required:
                fabric_info["status"] = "unknown"
                fabric_info["message"] = "未检测到 Fabric Loader 版本信息"
            elif not old_fabric:
                fabric_info["status"] = "unknown_old"
                fabric_info["message"] = f"未能检测到当前安装的 Fabric Loader 版本，模组要求 {new_fabric_required}+"
            elif not new_fabric_required:
                fabric_info["status"] = "unknown_new"
                fabric_info["message"] = f"当前安装 Fabric Loader {old_fabric}，未能检测到模组的加载器版本要求"
            else:
                old_t = _ver_tuple_f(old_fabric)
                new_t = _ver_tuple_f(new_fabric_required)
                if old_t < new_t:
                    fabric_info["status"] = "too_low"
                    fabric_info["message"] = (
                        f"Fabric Loader 版本不足！当前 {old_fabric}，模组要求 {new_fabric_required}+"
                    )
                elif old_t[0] > new_t[0]:
                    fabric_info["status"] = "too_high"
                    fabric_info["message"] = (
                        f"注意：当前 Fabric Loader {old_fabric} 主版本高于模组要求的 {new_fabric_required}，"
                        f"部分模组可能不兼容"
                    )
                else:
                    fabric_info["message"] = f"Fabric Loader 版本兼容（当前 {old_fabric}，要求 {new_fabric_required}+）"
        except Exception as e:
            fabric_info = {"status": "error", "message": f"版本检测失败: {e}", "demanding_mods": []}
            pass

        # 缓存旧目录扫描结果，更新时备份用户配置可以直接复用，省一次全盘哈希
        self._last_old_scan = old_files

        return {
            "added": added,
            "modified": modified,
            "removed": removed,
            "risky_removed": risky_removed,
            "removed_mods": removed_mods,
            "added_mods": added_mods,
            "preserve_modified": preserve_modified,
            "preserve_config": self.preserve_config,
            "delete_removed": self.delete_removed,
            "total_size": total_size,
            "total_count": len(added) + len(modified),
            "old_count": len(old_files),
            "new_count": len(new_files),
            "neoforge": neoforge_info,
            "fabric": fabric_info,
            "mrpack": self.mrpack_meta,
        }

    def _backup_before_update(self, changes: Dict) -> str:
        """
        更新前备份（用于回退）
        备份内容：
        - 被修改的文件（旧版本）
        - 被删除的文件
        - 用户修改过的保留配置文件
        - 新增文件列表（回退时删除）
        :return: 备份目录名（时间戳）
        """
        added_files = changes.get("added", [])
        modified_files = changes.get("modified", [])
        removed_files = changes.get("removed", [])
        preserve_modified = set(changes.get("preserve_modified", []))

        # 磁盘空间预检查：备份大约要占用「被修改 + 被删除」文件的体积，
        # 空间不足时提前报错，避免备份写一半就失败（那时用户会处于半备份状态）
        need_bytes = 0
        for rel_path in list(modified_files) + list(removed_files):
            try:
                f = self.old_dir / rel_path
                if f.is_file():
                    need_bytes += f.stat().st_size
            except OSError:
                continue
        if need_bytes > 0:
            try:
                free = shutil.disk_usage(str(self.updater_dir)).free
            except OSError:
                free = None
            if free is not None and free < need_bytes * 1.15:
                raise ValueError(
                    f"磁盘空间不足，无法完成更新前的安全备份。\n"
                    f"本次备份约需 {format_size(int(need_bytes * 1.15))}，"
                    f"当前可用 {format_size(free)}。\n"
                    f"请清理磁盘（或删除 .updater/backup 里的旧备份）后重试。"
                )

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_path = self.backup_dir / timestamp
        backup_path.mkdir(parents=True, exist_ok=True)

        self._log(f"创建备份: {timestamp}", "info")

        backup_manifest = {
            "backup_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "added_files": added_files,        # 本次新增的（回退时删除）
            "modified_files": modified_files,  # 本次修改的（回退时从备份恢复）
            "removed_files": removed_files,    # 本次删除的（回退时从备份恢复）
            "preserve_patterns": self.preserve_patterns
        }

        modified_count = 0
        removed_count = 0
        user_count = 0

        # 备份进度（大整合包备份几个 GB 时，界面不至于像卡死）
        backup_total = len(modified_files) + len(removed_files)
        backup_done = 0

        def _tick(msg: str):
            nonlocal backup_done
            backup_done += 1
            if backup_total > 20 and (backup_done % 20 == 0 or backup_done == backup_total):
                self._progress(backup_done, backup_total, msg)

        # 1. 备份被修改的文件（旧版本，从旧目录复制）
        for rel_path in modified_files:
            # 保留文件如果被用户修改过，单独备份到 user_files
            if rel_path in preserve_modified:
                src = self.old_dir / rel_path
                if src.exists() and src.is_file():
                    dst = backup_path / "user_files" / rel_path
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(src, dst)
                        user_count += 1
                    except (IOError, OSError) as e:
                        self._log(f"备份用户配置失败 {rel_path}: {e}", "warning")
                _tick(f"备份用户配置：{rel_path}")
                continue

            # 普通修改的文件，备份旧版本
            src = self.old_dir / rel_path
            if src.exists() and src.is_file():
                dst = backup_path / "modified_files" / rel_path
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                    modified_count += 1
                except (IOError, OSError) as e:
                    self._log(f"备份修改文件失败 {rel_path}: {e}", "warning")
            _tick(f"备份修改文件：{rel_path}")

        # 2. 备份被删除的文件（保留文件除外）
        for rel_path in removed_files:
            if self._match_preserve(rel_path):
                # 保留的配置文件也备份一下
                src = self.old_dir / rel_path
                if src.exists() and src.is_file():
                    dst = backup_path / "user_files" / rel_path
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(src, dst)
                        user_count += 1
                    except (IOError, OSError) as e:
                        self._log(f"备份用户配置失败 {rel_path}: {e}", "warning")
                _tick(f"备份用户配置：{rel_path}")
                continue

            src = self.old_dir / rel_path
            if src.exists() and src.is_file():
                dst = backup_path / "removed_files" / rel_path
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                    removed_count += 1
                except (IOError, OSError) as e:
                    self._log(f"备份删除文件失败 {rel_path}: {e}", "warning")
            _tick(f"备份删除文件：{rel_path}")

        # 3. 还要备份用户修改过、但本次更新没涉及到的保留文件
        # （保证回退后用户配置完整）
        all_user_modified = self.get_user_modified_files(scanned=self._last_old_scan)
        for rel_path in all_user_modified:
            if rel_path in preserve_modified:
                continue  # 已经备份过了
            src = self.old_dir / rel_path
            if src.exists() and src.is_file():
                dst = backup_path / "user_files" / rel_path
                if dst.exists():
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                    user_count += 1
                except (IOError, OSError) as e:
                    self._log(f"备份用户配置失败 {rel_path}: {e}", "warning")

        backup_manifest["modified_count"] = modified_count
        backup_manifest["removed_count"] = removed_count
        backup_manifest["user_count"] = user_count

        with open(backup_path / "backup_manifest.json", "w", encoding="utf-8") as f:
            json.dump(backup_manifest, f, ensure_ascii=False, indent=2)

        self._log(
            f"备份完成: 修改文件 {modified_count} 个, 删除文件 {removed_count} 个, 用户配置 {user_count} 个",
            "info"
        )
        return timestamp

    def get_backup_list(self) -> List[Dict]:
        """获取可用的备份列表（从新到旧）"""
        if not self.backup_dir.exists():
            return []

        backups = []
        for item in self.backup_dir.iterdir():
            manifest_file = item / "backup_manifest.json"
            if item.is_dir() and manifest_file.exists():
                try:
                    with open(manifest_file, "r", encoding="utf-8") as f:
                        manifest = json.load(f)
                    backups.append({
                        "name": item.name,
                        "time": manifest.get("backup_time", item.name),
                        "user_files": manifest.get("user_count", manifest.get("user_modified_count", 0)),
                        "modified_files": manifest.get("modified_count", 0),
                        "removed_files": manifest.get("removed_count", 0)
                    })
                except (json.JSONDecodeError, IOError):
                    backups.append({
                        "name": item.name,
                        "time": item.name,
                        "user_files": 0,
                        "removed_files": 0
                    })

        backups.sort(key=lambda x: x["name"], reverse=True)
        return backups

    def backup_total_size(self) -> int:
        """所有备份占用的磁盘空间（字节）"""
        if not self.backup_dir.exists():
            return 0
        total = 0
        for root, _dirs, files in os.walk(self.backup_dir):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
        return total

    def keep_backups(self, keep: int = None) -> List[str]:
        """
        只保留最近 keep 份备份，其余删除（按目录名从新到旧排序）。
        备份体积很大，长期不清理会把磁盘占满。
        :return: 被删除的备份目录名列表
        """
        keep = self.BACKUP_KEEP if keep is None else keep
        backups = self.get_backup_list()  # 已按新→旧排序
        removed = []
        for item in backups[max(keep, 0):]:
            name = item["name"]
            try:
                shutil.rmtree(self.backup_dir / name, ignore_errors=True)
                removed.append(name)
            except OSError:
                continue

        # 顺手清掉没有清单的残缺备份目录（上次备份中断留下的）
        try:
            for child in self.backup_dir.iterdir():
                if not child.is_dir():
                    continue
                if not (child / "backup_manifest.json").exists():
                    shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass

        if removed:
            self._log(f"已清理 {len(removed)} 份旧备份（只保留最近 {keep} 份）", "info")
        return removed

    def do_update(self, changes: Optional[Dict] = None) -> Tuple[bool, str, str]:
        """
        执行更新
        :param changes: 预计算的差异，None 则自动计算
        :return: (成功与否, 消息, 备份名)
        """
        if not self.old_dir.exists():
            return False, f"旧整合包目录不存在: {self.old_dir}", ""
        if not self.new_dir.exists():
            return False, f"新整合包目录不存在: {self.new_dir}", ""

        if changes is None:
            changes = self.compare()

        added = changes.get("added", [])
        modified = changes.get("modified", [])
        removed = changes.get("removed", []) if self.delete_removed else []
        preserve_modified = set(changes.get("preserve_modified", []))

        # 没有差异就不必备份（否则会留下一堆空备份）
        if len(added) + len(modified) + len(removed) == 0:
            self._log("没有需要更新的文件", "info")
            return True, "已经是最新状态，无需更新", ""

        # 更新前空间预检：新增 + 修改的文件还要复制进旧整合包，
        # 空间不够就提前报错，免得写到一半失败留下「半更新」状态
        copy_bytes = 0
        for rel_path in added + modified:
            try:
                f = self.new_dir / rel_path
                if f.is_file():
                    copy_bytes += f.stat().st_size
            except OSError:
                continue
        if copy_bytes > 0:
            try:
                free = shutil.disk_usage(str(self.old_dir)).free
            except OSError:
                free = None
            if free is not None and free < copy_bytes * 1.1:
                raise ValueError(
                    f"磁盘空间不足，无法完成更新。\n"
                    f"本次需要写入约 {format_size(int(copy_bytes * 1.1))}，"
                    f"当前可用 {format_size(free)}。\n"
                    f"请清理磁盘后重试（也可以先删掉 .updater/backup 里的旧备份）。"
                )

        # 备份
        backup_name = self._backup_before_update(changes)

        total_ops = len(added) + len(modified) + len(removed)
        processed = 0

        self._log(f"开始更新: 新增 {len(added)}，修改 {len(modified)}，删除 {len(removed)}", "info")

        # 1. 新增和修改文件
        all_to_copy = added + modified
        for rel_path in all_to_copy:
            processed += 1
            src = self.new_dir / rel_path
            dst = self.old_dir / rel_path

            # 用户修改过的保留文件，跳过
            if rel_path in preserve_modified:
                self._log(f"保留用户配置: {rel_path}", "info")
                self._progress(processed, total_ops, f"保留: {rel_path}")
                continue

            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            except (IOError, OSError) as e:
                self._log(f"复制失败 {rel_path}: {e}", "error")
                return (
                    False,
                    f"更新失败: {rel_path}\n原因: {e}\n\n"
                    f"已经更新过的文件可能处于「半更新」状态，"
                    f"可以在「版本回退」里回退到 {backup_name} 恢复。",
                    backup_name,
                )

            self._progress(processed, total_ops, f"更新: {rel_path}")

        # 2. 删除文件
        for rel_path in removed:
            processed += 1
            dst = self.old_dir / rel_path

            # 保留文件不删
            if self._match_preserve(rel_path):
                self._progress(processed, total_ops, f"保留: {rel_path}")
                continue

            if dst.exists():
                try:
                    dst.unlink()
                    self._log(f"删除: {rel_path}", "info")
                except IOError as e:
                    self._log(f"删除失败 {rel_path}: {e}", "warning")

            self._progress(processed, total_ops, f"删除: {rel_path}")

        # 3. 清理空目录
        self._clean_empty_dirs()

        # 4. 保存新的基准清单（新整合包的标准状态，用于下次对比判断用户修改）
        new_standard = self.scan_directory(self.new_dir)
        self.save_manifest(new_standard, version_label=backup_name)

        # 5. 只保留最近几份备份，避免备份无限堆积占满磁盘
        try:
            self.keep_backups()
        except OSError:
            pass

        self._progress(total_ops, total_ops, "更新完成!")
        self._log(
            f"更新完成! 新增 {len(added)}，修改 {len(modified)}，删除 {len(removed)}",
            "info"
        )

        return True, f"更新成功！已备份到 {backup_name}", backup_name

    def rollback(self, backup_name: str) -> Tuple[bool, str]:
        """
        回退到指定备份
        回退步骤：
        1. 删除本次新增的文件
        2. 从备份恢复被修改的文件（旧版本）
        3. 从备份恢复被删除的文件
        4. 从备份恢复用户修改的配置文件
        :param backup_name: 备份目录名
        :return: (成功与否, 消息)
        """
        backup_path = self.backup_dir / backup_name
        manifest_file = backup_path / "backup_manifest.json"

        if not manifest_file.exists():
            return False, f"找不到备份: {backup_name}"

        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            return False, f"读取备份清单失败: {e}"

        self._log(f"开始回退到备份: {backup_name}", "info")

        added_files = manifest.get("added_files", [])
        modified_files = manifest.get("modified_files", [])
        removed_files = manifest.get("removed_files", [])

        # 1. 删除本次新增的文件
        self._log(f"删除新增文件: {len(added_files)} 个", "info")
        for rel_path in added_files:
            dst = self.old_dir / rel_path
            if dst.exists() and dst.is_file():
                try:
                    dst.unlink()
                    self._log(f"删除新增文件: {rel_path}", "info")
                except IOError as e:
                    self._log(f"删除失败 {rel_path}: {e}", "warning")

        # 2. 恢复被修改的文件（从 modified_files 备份）
        modified_backup_dir = backup_path / "modified_files"
        if modified_backup_dir.exists() and modified_files:
            self._log(f"恢复修改文件: {len(modified_files)} 个", "info")
            for rel_path in modified_files:
                src = modified_backup_dir / rel_path
                if not src.exists():
                    self._log(f"备份文件不存在: {rel_path}", "warning")
                    continue
                dst = self.old_dir / rel_path
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    self._log(f"恢复修改文件: {rel_path}", "info")
                except IOError as e:
                    self._log(f"恢复失败 {rel_path}: {e}", "warning")

        # 3. 恢复被删除的文件（从 removed_files 备份）
        removed_backup_dir = backup_path / "removed_files"
        if removed_backup_dir.exists() and removed_files:
            self._log(f"恢复删除文件: {len(removed_files)} 个", "info")
            for rel_path in removed_files:
                src = removed_backup_dir / rel_path
                if not src.exists():
                    self._log(f"备份文件不存在: {rel_path}", "warning")
                    continue
                dst = self.old_dir / rel_path
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    self._log(f"恢复删除文件: {rel_path}", "info")
                except IOError as e:
                    self._log(f"恢复失败 {rel_path}: {e}", "warning")

        # 4. 恢复用户修改的配置文件（从 user_files 备份）
        user_backup_dir = backup_path / "user_files"
        if user_backup_dir.exists():
            for root, dirs, files in os.walk(user_backup_dir):
                root_path = Path(root)
                rel_root = root_path.relative_to(user_backup_dir)
                for file in files:
                    rel_path = str(rel_root / file).replace("\\", "/")
                    if rel_path.startswith("./"):
                        rel_path = rel_path[2:]
                    src = root_path / file
                    dst = self.old_dir / rel_path
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                        self._log(f"恢复用户配置: {rel_path}", "info")
                    except IOError as e:
                        self._log(f"恢复配置失败 {rel_path}: {e}", "warning")

        # 5. 清理空目录
        self._clean_empty_dirs()

        # 6. 更新基准清单
        # 重新扫描当前目录作为基准
        current_files = self.scan_directory(self.old_dir)
        self.save_manifest(current_files, version_label=f"rollback_{backup_name}")

        self._log(f"回退完成: {backup_name}", "info")
        return True, f"已回退到备份 {backup_name}"

    def rollback_files(self, backup_name: str, files_to_rollback: List[str]) -> Tuple[bool, str, int]:
        """
        回退指定的文件（从最近一次备份中恢复选中的文件）
        :param backup_name: 备份目录名
        :param files_to_rollback: 要回退的文件相对路径列表
        :return: (成功与否, 消息, 成功回退的文件数)
        """
        backup_path = self.backup_dir / backup_name
        manifest_file = backup_path / "backup_manifest.json"

        if not manifest_file.exists():
            return False, f"找不到备份: {backup_name}", 0

        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            return False, f"读取备份清单失败: {e}", 0

        added_files = set(manifest.get("added_files", []))
        modified_files = set(manifest.get("modified_files", []))
        removed_files = set(manifest.get("removed_files", []))

        success_count = 0
        self._log(f"开始回退选中的 {len(files_to_rollback)} 个文件", "info")

        modified_backup_dir = backup_path / "modified_files"
        removed_backup_dir = backup_path / "removed_files"

        for rel_path in files_to_rollback:
            try:
                if rel_path in added_files:
                    # 新增的文件：删除它
                    dst = self.old_dir / rel_path
                    if dst.exists() and dst.is_file():
                        dst.unlink()
                        success_count += 1
                        self._log(f"回退（删除新增）: {rel_path}", "info")

                elif rel_path in modified_files:
                    # 修改的文件：从备份恢复旧版本
                    src = modified_backup_dir / rel_path
                    if src.exists():
                        dst = self.old_dir / rel_path
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                        success_count += 1
                        self._log(f"回退（恢复旧版）: {rel_path}", "info")

                elif rel_path in removed_files:
                    # 删除的文件：从备份恢复
                    src = removed_backup_dir / rel_path
                    if src.exists():
                        dst = self.old_dir / rel_path
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                        success_count += 1
                        self._log(f"回退（恢复删除）: {rel_path}", "info")
                else:
                    self._log(f"跳过（不在本次更新中）: {rel_path}", "warning")
            except IOError as e:
                self._log(f"回退失败 {rel_path}: {e}", "warning")

        # 清理空目录
        self._clean_empty_dirs()

        # 更新基准清单
        current_files = self.scan_directory(self.old_dir)
        self.save_manifest(current_files, version_label=f"partial_rollback_{backup_name}")

        self._log(f"部分回退完成，成功 {success_count} 个文件", "info")
        return True, f"已回退 {success_count} 个文件", success_count

    def _clean_empty_dirs(self):
        """清理空目录"""
        for root, dirs, files in os.walk(self.old_dir, topdown=False):
            root_path = Path(root)
            if ".updater" in root_path.parts:
                continue
            if root_path == self.old_dir:
                continue
            try:
                if not any(root_path.iterdir()):
                    root_path.rmdir()
            except OSError:
                pass


def format_size(size_bytes: int) -> str:
    """格式化文件大小"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
