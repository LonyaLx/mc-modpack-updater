# -*- coding: utf-8 -*-
"""
兼容性自检脚本（更新器自带）

作用：把各种真实世界里的整合包结构 / 压缩包结构丢给更新器，
检查它能不能正确识别、并且绝对不碰用户数据。

用法（需已安装 Python 3.8+，无需第三方库）：
    python 自检-兼容性.py

全部通过会打印「全部通过」，否则列出失败项并以非 0 退出码结束。
"""
import json
import shutil
import struct
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from simple_updater import SimpleUpdater, _prepare_new_pack  # noqa: E402
from simple_updater import _safe_rel_name, _fix_zip_name  # noqa: E402

FAIL = []
work = Path(tempfile.mkdtemp(prefix="mc_updater_selfcheck_"))


def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (("  -> " + str(extra)) if extra else ""))
    if not cond:
        FAIL.append(name)


def mkpack(root, tag="v1"):
    (root / "mods").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "mods" / f"mod-{tag}.jar").write_bytes(b"JAR" + tag.encode())
    (root / "config" / "common.toml").write_text(tag, encoding="utf-8")
    return root


def resolve(path):
    return SimpleUpdater._resolve_pack_root(path)[0]


def make_zip_raw(path, entries):
    """按原始字节写文件名（不带 UTF-8 标志），用来模拟 GBK 中文压缩包"""
    out, central = bytearray(), bytearray()
    for raw_name, data in entries:
        crc = zlib.crc32(data) & 0xFFFFFFFF
        off = len(out)
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, 0, 0, 0,
                           crc, len(data), len(data), len(raw_name), 0)
        out += raw_name + data
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, 0, 0, 0,
                               crc, len(data), len(data), len(raw_name),
                               0, 0, 0, 0, 0, off)
        central += raw_name
    cd = len(out)
    out += central + struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries),
                                 len(entries), len(central), cd, 0)
    path.write_bytes(bytes(out))


def zipped(name, entries, raw=False):
    z = work / f"{name}.zip"
    if raw:
        make_zip_raw(z, entries)
    else:
        with zipfile.ZipFile(z, "w") as zf:
            for a, d in entries:
                zf.writestr(a, d)
    raw_root = _prepare_new_pack(z)[0]
    return SimpleUpdater._resolve_pack_root(raw_root)[0]


print("\n[1] 各种启动器 / 整合包目录结构")
i = mkpack(work / "prism" / "instances" / "P" / ".minecraft")
inst = work / "prism" / "instances" / "P"
(inst / "instance.cfg").write_text("x", encoding="utf-8")
(inst / "mmc-pack.json").write_text("{}", encoding="utf-8")
check("PrismLauncher / MultiMC 实例 → 进入 .minecraft", resolve(inst) == inst / ".minecraft")

p = mkpack(work / "mr" / "profiles" / "P")
check("Modrinth App 实例 → 原地", resolve(p) == p)

p = mkpack(work / "cf" / "Instances" / "P")
(p / "minecraftinstance.json").write_text("{}", encoding="utf-8")
check("CurseForge 客户端实例 → 原地", resolve(p) == p)

mc = work / "hmcl" / ".minecraft"
p = mkpack(mc / "versions" / "P")
check("HMCL / PCL 版本隔离 → versions/P", resolve(mc) == p)

p = mkpack(work / "vanilla" / ".minecraft")
check("官方启动器（无隔离）→ 原地", resolve(p) == p)

mc2 = work / "empty" / ".minecraft"
(mc2 / "mods").mkdir(parents=True)          # 残留的空 mods 目录
(mc2 / "libraries").mkdir(parents=True)
p = mkpack(mc2 / "versions" / "P")
check("残留空 mods 目录不会误导 → versions/P", resolve(mc2) == p)

print("\n[2] 解压出来的各种包装结构")
r = zipped("b2", [("P/mods/a.jar", b"A"), ("P/config/c.toml", b"C"),
                  ("README.txt", b"x"), ("LICENSE", b"y")])
check("单层目录 + 根目录 README → 自动下钻", (r / "mods" / "a.jar").is_file(), r)

r = zipped("b3", [("P/mods/a.jar", b"A"), ("P/config/c.toml", b"C"),
                  ("__MACOSX/._P", b"j"), (".DS_Store", b"j")])
check("macOS 打包（__MACOSX）→ 自动下钻", (r / "mods" / "a.jar").is_file(), r)

r = zipped("b4", [("outer/P/mods/a.jar", b"A"), ("outer/P/config/c.toml", b"C")])
check("多套两层 → 自动下钻两层", (r / "mods" / "a.jar").is_file(), r)

r = zipped("b5", [("manifest.json", b"{}"), ("modlist.html", b"<html>"),
                  ("overrides/mods/a.jar", b"A"), ("overrides/config/c.toml", b"C")])
check("CurseForge overrides 外壳 → overrides/", (r / "mods" / "a.jar").is_file(), r)

r = zipped("b7", [("天工创世/mods/模组.jar".encode("gbk"), b"A"),
                  ("天工创世/config/配置.toml".encode("gbk"), b"C")], raw=True)
check("GBK 中文文件名被还原",
      (r / "mods" / "模组.jar").is_file() and (r / "config" / "配置.toml").is_file(), r)

print("\n[3] 用户数据绝对不能动")
old = mkpack(work / "u_old", "1")
(old / "saves" / "我的世界").mkdir(parents=True)
(old / "saves" / "我的世界" / "level.dat").write_bytes(b"MINE")
(old / "journeymap").mkdir(parents=True)
(old / "journeymap" / "data.bin").write_bytes(b"MAP")
(old / "resourcepacks").mkdir(parents=True)
(old / "resourcepacks" / "我的材质.zip").write_bytes(b"RP")
new = mkpack(work / "u_new", "2")
for sub in ("saves/我的世界/level.dat", "journeymap/data.bin", "resourcepacks/我的材质.zip"):
    f = new / sub
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"PACK-VERSION")
up = SimpleUpdater(str(old), str(new))
ch = up.compare()
touched = [q for q in ch["added"] + ch["modified"] + ch["removed"]
           if q.startswith(("saves/", "journeymap/", "resourcepacks/"))]
check("存档 / 小地图 / 自己的资源包都不参与更新", touched == [], touched)
up.do_update(ch)
check("更新后存档没被改", (old / "saves" / "我的世界" / "level.dat").read_bytes() == b"MINE")
check("更新后小地图数据还在", (old / "journeymap" / "data.bin").read_bytes() == b"MAP")
check("更新后自己的资源包还在", (old / "resourcepacks" / "我的材质.zip").read_bytes() == b"RP")

old2 = mkpack(work / "r_old", "1")
new2 = mkpack(work / "r_new", "2")
(new2 / "resourcepacks").mkdir(parents=True)
(new2 / "resourcepacks" / "作者材质.zip").write_bytes(b"PACK")
up2 = SimpleUpdater(str(old2), str(new2))
up2.do_update(up2.compare())
check("整合包自带的新资源包会正常安装", (old2 / "resourcepacks" / "作者材质.zip").exists())

print("\n[4] 服务端整合包")
old3 = mkpack(work / "s_old", "1")
(old3 / "world").mkdir(parents=True)
(old3 / "world" / "level.dat").write_bytes(b"WORLD")
(old3 / "server.properties").write_text("motd=mine", encoding="utf-8")
new3 = mkpack(work / "s_new", "2")
(new3 / "world").mkdir(parents=True)
(new3 / "world" / "level.dat").write_bytes(b"PACK")
(new3 / "server.properties").write_text("motd=pack", encoding="utf-8")
up3 = SimpleUpdater(str(old3), str(new3))
ch3 = up3.compare()
check("服务端 world / server.properties 不参与更新",
      [q for q in ch3["added"] + ch3["modified"] + ch3["removed"]
       if q.startswith("world/") or q == "server.properties"] == [])
up3.do_update(ch3)
check("服务端世界没被覆盖", (old3 / "world" / "level.dat").read_bytes() == b"WORLD")
check("服主配置没被覆盖", (old3 / "server.properties").read_text(encoding="utf-8") == "motd=mine")

print("\n[5] 新旧结构不一致（文件 ↔ 目录）")
o, n = work / "t1_old", work / "t1_new"
(o / "mods").mkdir(parents=True)
(o / "mods" / "thing").write_bytes(b"FILE")
(n / "mods" / "thing").mkdir(parents=True)
(n / "mods" / "thing" / "x.jar").write_bytes(b"X")
up4 = SimpleUpdater(str(o), str(n))
ok4, _, _ = up4.do_update()
check("旧=文件 新=目录 → 更新成功", ok4 and (o / "mods" / "thing" / "x.jar").is_file())

o, n = work / "t2_old", work / "t2_new"
(o / "mods" / "thing").mkdir(parents=True)
(o / "mods" / "thing" / "x.jar").write_bytes(b"X")
(n / "mods").mkdir(parents=True)
(n / "mods" / "thing").write_bytes(b"FILE")
SimpleUpdater(str(o), str(n)).do_update()
check("旧=目录 新=文件 → 结果是文件", (o / "mods" / "thing").is_file())

print("\n[6] 用户手动添加的文件")
o = mkpack(work / "ua_old", "1")
SimpleUpdater(str(o), str(mkpack(work / "ua_new1", "2"))).do_update()
(o / "mods" / "我加的.jar").write_bytes(b"MINE")
up6 = SimpleUpdater(str(o), str(mkpack(work / "ua_new2", "3")))
ch6 = up6.compare()
check("被识别为「用户自己加的」", "mods/我加的.jar" in ch6.get("user_added", []))
check("不在删除列表里", "mods/我加的.jar" not in ch6["removed"])
up6.do_update(ch6)
check("更新后仍在", (o / "mods" / "我加的.jar").exists())

print("\n[7] 恶意路径 / 文件名处理")
work2 = work / "evil"
work2.mkdir()
z = work2 / "evil.zip"
make_zip_raw(z, [(b"../evil.jar", b"E"), (b"/abs/evil.jar", b"E"),
                 (b"mods/../../evil.jar", b"E"), (b"C:/windows/evil.jar", b"E")])
_prepare_new_pack(z)
check("压缩包路径穿越被挡住", not (work2 / "evil.jar").exists()
      and not (work / "evil.jar").exists() and not (work2.parent / "evil.jar").exists())
check("_safe_rel_name 拒绝非法路径",
      _safe_rel_name("../a") is None and _safe_rel_name("/a") is None
      and _safe_rel_name("C:/a") is None and _safe_rel_name("mods/a.jar") == "mods/a.jar")
check("_fix_zip_name 还原 GBK 中文名",
      _fix_zip_name("天工创世/mods/模组.jar".encode("gbk").decode("cp437"), 0)
      == "天工创世/mods/模组.jar")
check("_fix_zip_name 不影响普通英文 / 西文名",
      _fix_zip_name("Pokemon/mods/a.jar", 0) == "Pokemon/mods/a.jar"
      and _fix_zip_name("Pokémon/a.jar", 0) == "Pokémon/a.jar")

print("\n[8] .mrpack 更新时复用旧整合包里已有的模组（不重复下载）")
import hashlib  # noqa: E402


def _sha1(data):
    return hashlib.sha1(data).hexdigest()


def _make_mrpack(path, files, overrides=None):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("modrinth.index.json", json.dumps({
            "name": "自检包", "versionId": "1.0",
            "dependencies": {"minecraft": "1.21.1", "neoforge": "21.1.0"},
            "files": files,
        }, ensure_ascii=False))
        for arc, data in (overrides or {}).items():
            zf.writestr(arc, data)


DEAD = "http://127.0.0.1:9/unreachable.jar"     # 不可达：一旦真的去下载就会失败

rwork = work / "reuse"
rwork.mkdir()
oldr = rwork / "old"
(oldr / "mods").mkdir(parents=True)
(oldr / "config").mkdir(parents=True)
CONTENT_A = b"MOD-A" + b"a" * 400
CONTENT_B = b"MOD-B" + b"b" * 700
(oldr / "mods" / "a.jar").write_bytes(CONTENT_A)
(oldr / "mods" / "b.jar").write_bytes(CONTENT_B)
(oldr / "config" / "c.toml").write_text("v1", encoding="utf-8")

mp = rwork / "reuse.mrpack"
_make_mrpack(mp, [
    {"path": "mods/a.jar", "hashes": {"sha1": _sha1(CONTENT_A)},
     "fileSize": len(CONTENT_A), "downloads": [DEAD]},
    {"path": "mods/b.jar", "hashes": {"sha1": _sha1(CONTENT_B)},
     "fileSize": len(CONTENT_B), "downloads": [DEAD]},
], overrides={"overrides/config/c.toml": b"v2"})

upr = SimpleUpdater(str(oldr), str(mp))
mpr = upr.mrpack_meta or {}
check("旧包里已有的模组被复用（2 个）", mpr.get("reused_local") == 2, mpr)
check("全部复用 → 一个都不需要下载", mpr.get("downloaded") == 0, mpr)
check("省下的下载量统计正确", mpr.get("reused_bytes") == len(CONTENT_A) + len(CONTENT_B), mpr)
chr_ = upr.compare()
check("内容一致的模组不算「要更新」",
      not [q for q in chr_["added"] + chr_["modified"] if q.startswith("mods/")],
      chr_["added"] + chr_["modified"])
okr, _, _ = upr.do_update(chr_)
check("完全离线也能完成更新", okr)
check("复用后旧包文件内容完好", (oldr / "mods" / "a.jar").read_bytes() == CONTENT_A
      and (oldr / "mods" / "b.jar").read_bytes() == CONTENT_B)

# 同名、同大小但内容不同 → 必须靠哈希判定为不可复用（会去下载并失败）
oldr2 = rwork / "old2"
shutil.copytree(oldr, oldr2)
SAME_LEN_OTHER = b"XXXXXX" + b"z" * (len(CONTENT_A) - 6)
mp2 = rwork / "diff.mrpack"
_make_mrpack(mp2, [
    {"path": "mods/a.jar", "hashes": {"sha1": _sha1(SAME_LEN_OTHER)},
     "fileSize": len(SAME_LEN_OTHER), "downloads": [DEAD]},
], overrides={})
try:
    SimpleUpdater(str(oldr2), str(mp2))
    check("同名同大小但内容不同 → 不复用", False, "被错误复用了")
except ValueError as e:
    check("同名同大小但内容不同 → 不复用（走下载）", "下载失败" in str(e), str(e)[:60])
check("不可复用时旧文件没被改坏", (oldr2 / "mods" / "a.jar").read_bytes() == CONTENT_A)

print("\n[9] 新版本包里没有模组文件时怎么处理")
cf_work = work / "cfonly"
cf_work.mkdir()
oldcf = cf_work / "old"
(oldcf / "mods").mkdir(parents=True)
(oldcf / "config").mkdir(parents=True)
(oldcf / "mods" / "keep.jar").write_bytes(b"KEEP")
(oldcf / "config" / "c.toml").write_text("v1", encoding="utf-8")

CF_FILES = [{"projectID": 238222, "fileID": 4593541, "required": True},
            {"projectID": 306770, "fileID": 5100000, "required": False}]


def _cf_manifest(files):
    return json.dumps({
        "minecraft": {"version": "1.21.1",
                      "modLoaders": [{"id": "neoforge-21.1.0", "primary": True}]},
        "manifestType": "minecraftModpack", "manifestVersion": 1,
        "name": "自检包", "version": "1.0", "files": files, "overrides": "overrides",
    }, ensure_ascii=False)


# ① CurseForge 清单包：只有 manifest.json + overrides，没有 mods → 必须拦住
z_cf = cf_work / "清单包.zip"
with zipfile.ZipFile(z_cf, "w") as zf:
    zf.writestr("manifest.json", _cf_manifest(CF_FILES))
    zf.writestr("overrides/config/c.toml", b"v2")
cf_logs = []
try:
    SimpleUpdater(str(oldcf), str(z_cf), log_callback=lambda m, l="info": cf_logs.append(m))
    check("CurseForge 清单包被拦住", False, "没有报错")
except ValueError as e:
    t = str(e)
    cls_log = "\n".join(cf_logs)
    check("CurseForge 清单包被拦住", "清单包" in t, t.splitlines()[0])
    check("弹窗说明 CurseForge 需要密钥、工具补不了", "密钥" in t)
    check("弹窗直接给了最省事的办法", "最省事的办法" in t and "用启动器" in t)
    check("日志里给出 3 种出路（启动器 / 完整包 / 手动补）",
          "用启动器装一次" in cls_log and "完整包" in cls_log and "projectID" in cls_log)
check("生成了「需要手动补的模组清单」文件",
      (cf_work / "清单包.需要手动补的模组清单.txt").is_file())

# ② 同格式但模组已打全（第三方重打包）→ 正常放行
z_full = cf_work / "完整重打包.zip"
with zipfile.ZipFile(z_full, "w") as zf:
    zf.writestr("manifest.json", _cf_manifest(CF_FILES))
    zf.writestr("overrides/mods/full.jar", b"FULL")
up_full = SimpleUpdater(str(oldcf), str(z_full))
ch_full = up_full.compare()
check("模组打全的 CurseForge 包正常放行", "mods/full.jar" in ch_full["added"], ch_full["added"])

# ③ 完全没有整合包内容的目录 → 拦住（避免把旧包清空）
empty_dir = cf_work / "空目录"
empty_dir.mkdir()
try:
    SimpleUpdater(str(oldcf), str(empty_dir))
    check("空目录被拦住", False, "没有报错")
except ValueError as e:
    check("空目录被拦住", "没找到任何整合包内容" in str(e))
check("被拦住时旧包没被改动", (oldcf / "mods" / "keep.jar").read_bytes() == b"KEEP")

print("\n==== 结果 ====")
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("全部通过")
