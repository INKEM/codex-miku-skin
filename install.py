#!/usr/bin/env python3
"""Version-pinned, reversible CSS-only Codex skin. Python 3.11+ and tomlkit.

Usage: python3 install.py {check,prepare,install,prepare-update,update,status,uninstall} [options]
Backups belong in a private state directory, never in a shared output bundle.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import struct
import sys
import tempfile

PACKAGE = Path(__file__).resolve().parent
MARKER = 'id="miku-dark-skin-v1"'
KEYS = ("appearanceTheme", "appearanceDarkChromeTheme", "appearanceDarkCodeThemeId")


class SkinError(Exception):
    pass


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path, data, mode=0o600, owner=None):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".miku-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        if owner and os.geteuid() == 0:
            os.chown(name, *owner)
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_copy(source, target, expected=None):
    target = Path(target)
    metadata = target.stat() if target.exists() else Path(source).stat()
    fd, name = tempfile.mkstemp(prefix=".miku-asar-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out, Path(source).open("rb") as inp:
            shutil.copyfileobj(inp, out, 4 * 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        if expected and digest(name) != expected:
            raise SkinError("复制校验失败；未替换原资源。")
        os.chmod(name, stat.S_IMODE(metadata.st_mode))
        if os.geteuid() == 0:
            os.chown(name, metadata.st_uid, metadata.st_gid)
        os.replace(name, target)
        sync_dir(target.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Asar:
    def __init__(self, path):
        self.path = Path(path)
        with self.path.open("rb") as stream:
            raw = stream.read(16)
            if len(raw) != 16:
                raise SkinError("ASAR 文件头不完整。")
            first, header_size, payload_size, string_size = struct.unpack("<4I", raw)
            if first != 4 or header_size != payload_size + 4 or not 0 < string_size < 32_000_000:
                raise SkinError("无法识别 ASAR 文件头。")
            self.header_size = header_size
            self.base = 8 + header_size
            self.root = json.loads(stream.read(string_size))
        if self.base > self.path.stat().st_size:
            raise SkinError("ASAR 文件被截断。")

    def entry(self, name):
        obj = self.root
        for component in name.split("/"):
            obj = obj["files"][component]
        if "offset" not in obj or obj.get("unpacked") or "link" in obj:
            raise SkinError("目标不是已打包的普通资源。")
        return obj

    def read(self, name):
        obj = self.entry(name)
        with self.path.open("rb") as stream:
            stream.seek(self.base + int(obj["offset"]))
            raw = stream.read(obj["size"])
        if len(raw) != obj["size"]:
            raise SkinError("ASAR 资源长度错误。")
        return raw

    def version(self):
        return json.loads(self.read("package.json"))["version"]


def assets():
    compatibility = load_json(PACKAGE / "compatibility.json")
    css = (PACKAGE / "miku-dark.css").read_text(encoding="utf-8")
    if re.search(r"url\s*\(|@import\b|</style|<svg\b|data:image", css, re.I):
        raise SkinError("样式必须完全由本地 CSS 几何、配色和文字组成。")
    if compatibility.get("cssSha256") != hashlib.sha256(css.encode()).hexdigest():
        raise SkinError("样式校验失败；请使用原始安装包或重新生成已验证的包。")
    text = (PACKAGE / "miku.codex-theme").read_text(encoding="utf-8").strip()
    if not text.startswith("codex-theme-v1:"):
        raise SkinError("主题格式错误。")
    theme = json.loads(text[len("codex-theme-v1:"):])
    if theme["variant"] != "dark" or theme["codeThemeId"] != "codex":
        raise SkinError("主题必须使用暗色模式和 Codex Dark。")
    if compatibility.get("themeSha256") != hashlib.sha256(text.encode()).hexdigest():
        raise SkinError("主题校验失败。")
    return compatibility, css, theme


def validate_original(path, compatibility):
    archive = Asar(path)
    if archive.version() != compatibility["appVersion"]:
        raise SkinError(f"不支持应用版本 {archive.version()}；仅适配 {compatibility['appVersion']}。")
    if digest(path) != compatibility["asarSha256"]:
        raise SkinError("原应用资源哈希与适配版本不符；停止安装，避免覆盖其他修改或升级。")
    html = archive.read(compatibility["htmlPath"])
    if hashlib.sha256(html).hexdigest() != compatibility["htmlSha256"] or MARKER.encode() in html:
        raise SkinError("HTML 入口不匹配或已被修改。")
    return archive


def patch_archive(source, destination, compatibility, css):
    """Append HTML and update only its ASAR entry; all existing payload offsets stay valid."""
    archive = Asar(source)
    html = archive.read(compatibility["htmlPath"]).decode("utf-8")
    if html.count("</head>") != 1 or MARKER in html:
        raise SkinError("HTML 入口不支持此补丁。")
    new_html = html.replace("</head>", f'<style {MARKER}>\n{css}\n</style>\n  </head>').encode()
    obj = archive.entry(compatibility["htmlPath"])
    obj["offset"] = str(Path(source).stat().st_size - archive.base)
    obj["size"] = len(new_html)
    block_size = obj.get("integrity", {}).get("blockSize", 4194304)
    obj["integrity"] = {
        "algorithm": "SHA256", "hash": hashlib.sha256(new_html).hexdigest(),
        "blockSize": block_size,
        "blocks": [hashlib.sha256(new_html[i:i + block_size]).hexdigest()
                   for i in range(0, len(new_html), block_size)],
    }
    serialized = json.dumps(archive.root, ensure_ascii=False, separators=(",", ":")).encode()
    padding = b"\0" * (-len(serialized) % 4)
    header = struct.pack("<II", 4 + len(serialized) + len(padding), len(serialized)) + serialized + padding
    # This also protects the running app's cached offsets until the user restarts it.
    if len(header) != archive.header_size:
        raise SkinError("补丁将改变 ASAR 数据区起点；此版本需要重新适配。")
    with Path(source).open("rb") as inp, Path(destination).open("xb") as out:
        out.write(struct.pack("<II", 4, len(header)))
        out.write(header)
        inp.seek(archive.base)
        shutil.copyfileobj(inp, out, 4 * 1024 * 1024)
        out.write(new_html)
        out.flush()
        os.fsync(out.fileno())
    patched = Asar(destination)
    if patched.read(compatibility["htmlPath"]) != new_html:
        raise SkinError("补丁写入验证失败。")
    return digest(destination)


def toml_module():
    try:
        import tomlkit
        return tomlkit
    except ImportError:
        raise SkinError("缺少 tomlkit。请使用已安装该模块的 Python，或先执行 python3 -m pip install tomlkit。") from None


def read_config(path):
    tk = toml_module()
    path = Path(path)
    if path.is_symlink():
        raise SkinError("配置是符号链接；请通过 --config 指定真实文件。")
    text = path.read_text(encoding="utf-8")
    document = tk.parse(text)
    desktop = document.get("desktop", {})
    snapshot = {key: {"present": key in desktop, "value": desktop.get(key)} for key in KEYS}
    return text, document, json.loads(json.dumps(snapshot))


def without_nulls(value):
    if isinstance(value, dict):
        return {k: without_nulls(v) for k, v in value.items() if v is not None}
    return value


def desired_settings(theme, snapshot=None):
    result = {
        "appearanceTheme": "dark",
        "appearanceDarkChromeTheme": without_nulls(theme["theme"]),
        "appearanceDarkCodeThemeId": "codex",
    }
    if snapshot:
        previous = snapshot["appearanceDarkChromeTheme"]["value"] or {}
        if isinstance(previous.get("fonts"), dict):
            result["appearanceDarkChromeTheme"]["fonts"] = copy.deepcopy(previous["fonts"])
    return result


def render_config(document, changes):
    tk = toml_module()
    if "desktop" not in document:
        document["desktop"] = tk.table()
    def item(value):
        if isinstance(value, dict):
            table = tk.inline_table()
            for k, v in value.items():
                table[k] = item(v)
            return table
        return value
    for key, record in changes.items():
        if record["present"]:
            document["desktop"][key] = item(record["value"])
        else:
            document["desktop"].pop(key, None)
    rendered = tk.dumps(document)
    tk.parse(rendered)
    return rendered


def write_config(path, expected_text, document, changes):
    rendered = render_config(document, changes)
    # Avoid silently overwriting a write from the running app or another editor.
    if Path(path).read_text(encoding="utf-8") != expected_text:
        raise SkinError("配置在准备期间发生变化；未覆盖，请重新运行。")
    metadata = Path(path).stat()
    atomic_bytes(path, rendered.encode(), stat.S_IMODE(metadata.st_mode), (metadata.st_uid, metadata.st_gid))


def save_state(directory, state, name="state.json"):
    owner = directory.stat()
    atomic_bytes(directory / name, (json.dumps(state, indent=2, ensure_ascii=False) + "\n").encode(),
                 owner=(owner.st_uid, owner.st_gid))


@contextlib.contextmanager
def locked(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise SkinError("备份目录不能是符号链接。")
    os.chmod(directory, 0o700)
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def prepare(args, compatibility, css):
    state_path = args.state_dir / "state.json"
    state = load_json(state_path) if state_path.exists() else None
    current_hash = digest(args.app)
    if state:
        if state["app"] != str(args.app) or state["config"] != str(args.config):
            raise SkinError("备份属于另一套应用或配置；请使用单独的 --state-dir。")
        if current_hash == state["patchedSha256"]:
            if digest(args.state_dir / "original.asar") != state["originalSha256"]:
                raise SkinError("原始备份损坏；请先恢复有效备份。")
            if state["cssSha256"] != compatibility["cssSha256"]:
                raise SkinError("已安装不同版本皮肤；请运行 update 原位更新，保留最初的卸载记录。")
            return state, True
        if current_hash != state["originalSha256"]:
            raise SkinError("应用资源已改变；不会覆盖升级后的应用。")
        if state["originalSha256"] != compatibility["asarSha256"]:
            raise SkinError("备份与当前安装包版本不一致。")
    validate_original(args.app, compatibility)
    original = args.state_dir / "original.asar"
    patched = args.state_dir / (state.get("patchedFile", "patched.asar") if state else "patched.asar")
    if original.exists():
        if digest(original) != compatibility["asarSha256"]:
            raise SkinError("备份哈希不匹配；不会覆盖现有备份。")
    else:
        atomic_copy(args.app, original, compatibility["asarSha256"])
    if not (state and patched.exists() and digest(patched) == state["patchedSha256"]):
        temporary = args.state_dir / "patched.asar.building"
        temporary.unlink(missing_ok=True)
        try:
            patched_hash = patch_archive(original, temporary, compatibility, css)
            os.replace(temporary, patched)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        patched_hash = state["patchedSha256"]
    new_state = {
        "app": str(args.app), "config": str(args.config), "appVersion": compatibility["appVersion"],
        "originalSha256": compatibility["asarSha256"], "patchedSha256": patched_hash,
        "cssSha256": compatibility["cssSha256"], "status": "prepared",
        "patchedFile": patched.name,
    }
    # Keep recovery data after a partial installation, even if it was interrupted.
    if state and state.get("status") not in ("uninstalled", "prepared"):
        new_state.update({k: state[k] for k in ("previousSettings", "installedSettings", "status") if k in state})
    save_state(args.state_dir, new_state)
    return new_state, False


def install(args, state, already_installed, theme):
    text, document, snapshot = read_config(args.config)
    wanted = desired_settings(theme, snapshot)
    records = {k: {"present": True, "value": v} for k, v in wanted.items()}
    if already_installed:
        if state.get("status") == "installing":
            # Resume an interrupted transaction, without replacing subsequent user edits.
            changes = {k: records[k] for k in KEYS if snapshot[k] == state["previousSettings"][k]}
            if changes:
                write_config(args.config, text, document, changes)
            state["status"] = "installed"
            save_state(args.state_dir, state)
            print("已完成上次中断的安装；请手动重启 Codex。")
            return
        if state.get("status") == "installed":
            print("皮肤资源已安装；未重复修改资源或覆盖当前外观偏好。")
            state["status"] = "installed"
            save_state(args.state_dir, state)
            return
        raise SkinError("资源已打补丁但缺少安装前配置记录；请检查备份状态。")
    if not os.access(args.app.parent, os.W_OK) or not os.access(args.config.parent, os.W_OK):
        raise SkinError("没有原子替换应用资源或配置的权限。补丁和备份已准备完成；请按 README 的管理员安装命令继续。")
    # Persist rollback data before either target is changed.
    previous = state["previousSettings"] if state.get("status") == "installing" else snapshot
    state.update(status="installing", previousSettings=previous, installedSettings=records)
    save_state(args.state_dir, state)
    if digest(args.app) != state["originalSha256"]:
        raise SkinError("安装前资源发生变化；停止安装。")
    replaced = False
    try:
        atomic_copy(args.state_dir / state.get("patchedFile", "patched.asar"), args.app, state["patchedSha256"])
        replaced = True
        write_config(args.config, text, document, records)
        state["status"] = "installed"
        save_state(args.state_dir, state)
    except BaseException:
        if replaced:
            atomic_copy(args.state_dir / "original.asar", args.app, state["originalSha256"])
        # Restore only values that still equal our installed settings.
        restore_settings(args, state)
        state["status"] = "prepared"
        save_state(args.state_dir, state)
        raise
    print("MIKU / 01 已安装。请保存工作后手动退出并重新打开 Codex；不会自动结束当前会话。")


def prepare_update(args, compatibility, css):
    state_path = args.state_dir / "state.json"
    if not state_path.exists():
        raise SkinError("没有现有安装记录；首次安装请使用 install。")
    state = load_json(state_path)
    if state["app"] != str(args.app) or state["config"] != str(args.config):
        raise SkinError("备份属于另一套应用或配置。")
    if state.get("status") != "installed":
        raise SkinError("当前安装尚未完成或已卸载；请先完成安装。")
    original = args.state_dir / "original.asar"
    validate_original(original, compatibility)
    if state["originalSha256"] != compatibility["asarSha256"]:
        raise SkinError("最初备份与当前适配版本不一致。")
    current_hash = digest(args.app)
    if state["cssSha256"] == compatibility["cssSha256"] and current_hash == state["patchedSha256"]:
        return None
    previous_archive = args.state_dir / state.get("patchedFile", "patched.asar")
    if digest(previous_archive) != state["patchedSha256"]:
        raise SkinError("当前皮肤的回滚副本校验失败；停止更新。")
    pending_path = args.state_dir / "update.json"
    if pending_path.exists():
        pending = load_json(pending_path)
        if (pending["cssSha256"] == compatibility["cssSha256"]
                and pending["beforeState"] == state
                and digest(args.state_dir / pending["patchedFile"]) == pending["patchedSha256"]
                and current_hash in (state["patchedSha256"], pending["patchedSha256"])):
            return pending
    if current_hash != state["patchedSha256"]:
        raise SkinError("当前应用已升级或有未知修改，不能覆盖。")
    candidate = args.state_dir / f"revision-{compatibility['cssSha256'][:16]}.asar"
    temporary = candidate.with_suffix(".building")
    temporary.unlink(missing_ok=True)
    try:
        patched_hash = patch_archive(original, temporary, compatibility, css)
        os.replace(temporary, candidate)
    finally:
        temporary.unlink(missing_ok=True)
    pending = {"beforeState": state, "patchedSha256": patched_hash,
               "patchedFile": candidate.name, "cssSha256": compatibility["cssSha256"],
               "skinVersion": compatibility["skinVersion"]}
    save_state(args.state_dir, pending, "update.json")
    return pending


def update(args, compatibility, css):
    pending = prepare_update(args, compatibility, css)
    if pending is None:
        print("当前皮肤已经是此修订版；未重复修改资源或设置。")
        return
    before = pending["beforeState"]
    current_hash = digest(args.app)
    replaced = False
    try:
        if current_hash != pending["patchedSha256"]:
            if not os.access(args.app.parent, os.W_OK):
                raise SkinError("更新补丁已准备完成；写入系统目录需要管理员权限，请运行 install.sh update。")
            if current_hash != before["patchedSha256"]:
                raise SkinError("更新前应用资源发生变化。")
            atomic_copy(args.state_dir / pending["patchedFile"], args.app, pending["patchedSha256"])
            replaced = True
        after = {**before, "patchedSha256": pending["patchedSha256"],
                 "cssSha256": pending["cssSha256"], "patchedFile": pending["patchedFile"],
                 "skinVersion": pending["skinVersion"], "status": "installed"}
        save_state(args.state_dir, after)
    except BaseException:
        if replaced:
            atomic_copy(args.state_dir / before.get("patchedFile", "patched.asar"), args.app, before["patchedSha256"])
            save_state(args.state_dir, before)
        raise
    try:
        (args.state_dir / "update.json").unlink(missing_ok=True)
    except OSError:
        pass  # The committed state is authoritative; an obsolete pending record is harmless.
    print("皮肤已更新；原始备份、最初的卸载记录和现有外观配置均保留。请手动重启 Codex。")


def restore_settings(args, state):
    if "previousSettings" not in state:
        return []
    text, document, current = read_config(args.config)
    changes = {}
    skipped = []
    for key in KEYS:
        if current[key] == state["installedSettings"][key]:
            changes[key] = state["previousSettings"][key]
        elif current[key] != state["previousSettings"][key]:
            skipped.append(key)
    if changes:
        write_config(args.config, text, document, changes)
    return skipped


def uninstall(args):
    state_path = args.state_dir / "state.json"
    if not state_path.exists():
        print("此备份目录没有安装记录；未修改应用。")
        return
    state = load_json(state_path)
    if state["app"] != str(args.app) or state["config"] != str(args.config):
        raise SkinError("备份路径与目标不符。")
    current_hash = digest(args.app)
    if current_hash not in (state["originalSha256"], state["patchedSha256"]):
        raise SkinError("应用资源已被升级或改动；不会用旧版本备份覆盖。原始备份仍保留。")
    if digest(args.state_dir / "original.asar") != state["originalSha256"]:
        raise SkinError("原始备份校验失败；未修改资源和设置。")
    if current_hash == state["patchedSha256"]:
        if not os.access(args.app.parent, os.W_OK):
            raise SkinError("恢复系统资源需要管理员权限；请按 README 执行。")
        # Validate configuration before changing resources.
        read_config(args.config)
        atomic_copy(args.state_dir / "original.asar", args.app, state["originalSha256"])
    skipped = restore_settings(args, state)
    state["status"] = "uninstalled"
    save_state(args.state_dir, state)
    print("皮肤已卸载，原始资源已恢复；请手动重启 Codex。备份继续保留。")
    if skipped:
        print("保留安装后自行修改的外观设置：" + ", ".join(skipped))


def user_home():
    if os.environ.get("SUDO_UID"):
        return Path(pwd.getpwuid(int(os.environ["SUDO_UID"])).pw_dir)
    return Path.home()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "prepare", "install", "prepare-update", "update", "status", "uninstall"))
    parser.add_argument("--app", type=Path, default=Path("/usr/lib/chatgpt/resources/app.asar"))
    parser.add_argument("--config", type=Path, default=user_home() / ".codex/config.toml")
    parser.add_argument("--state-dir", type=Path, default=PACKAGE / ".state")
    args = parser.parse_args(argv)
    # Keep the application path lexical so a symlink cannot silently redirect an update.
    args.app = args.app.absolute()
    args.config = args.config.absolute()
    args.state_dir = args.state_dir.absolute()
    if args.app.is_symlink():
        raise SkinError("应用资源不能是符号链接。")
    compatibility, css, theme = assets()
    if args.command == "check":
        validate_original(args.app, compatibility)
        read_config(args.config)
        print(f"兼容检查通过：Codex {compatibility['appVersion']}；主题与样式校验通过。")
        return
    if args.command == "status":
        state_path = args.state_dir / "state.json"
        if not state_path.exists():
            print("尚未准备或安装。")
            return
        state = load_json(state_path)
        current_hash = digest(args.app)
        print(json.dumps({"recordedStatus": state["status"], "resources":
            "installed" if current_hash == state["patchedSha256"] else
            "original" if current_hash == state["originalSha256"] else "changed",
            "backupValid": digest(args.state_dir / "original.asar") == state["originalSha256"],
            "updateAvailable": state["cssSha256"] != compatibility["cssSha256"],
            "updatePrepared": (args.state_dir / "update.json").exists(),
            "stateDirectory": str(args.state_dir)}, indent=2, ensure_ascii=False))
        return
    with locked(args.state_dir):
        if args.command == "uninstall":
            uninstall(args)
            return
        if args.command == "prepare-update":
            pending = prepare_update(args, compatibility, css)
            print("更新补丁已准备完成；应用与配置尚未改动。" if pending else "当前皮肤已经是此修订版。")
            return
        if args.command == "update":
            update(args, compatibility, css)
            return
        state, already_installed = prepare(args, compatibility, css)
        if args.command == "prepare":
            print(f"备份与补丁已就绪：{args.state_dir}。尚未修改应用或配置。")
        else:
            install(args, state, already_installed, theme)


if __name__ == "__main__":
    try:
        main()
    except (SkinError, OSError, ValueError, KeyError) as error:
        print(f"MIKU / 01：{error}", file=sys.stderr)
        sys.exit(1)
