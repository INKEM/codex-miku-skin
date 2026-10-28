"""Transaction and archive tests; all mutations use temporary fixture files."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
import shutil
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("miku_installer", Path(__file__).with_name("install.py"))
skin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(skin)


def fixture_archive(path, version="26.1023.51421"):
    package = json.dumps({"version": version}).encode()
    html = b"<!doctype html><html><head>" + b" " * 15000 + b"</head><body>fixture</body></html>"
    def entry(offset, data):
        h = hashlib.sha256(data).hexdigest()
        return {"size": len(data), "offset": str(offset), "integrity": {
            "algorithm": "SHA256", "hash": h, "blockSize": 4194304, "blocks": [h]}}
    root = {"files": {"package.json": entry(0, package), "webview": {"files": {
        "index.html": entry(10000, html)}}, "untouched.bin": entry(10000 + len(html), b"UNTOUCHED" * 1500)}}
    data = package + b"\0" * (10000 - len(package)) + html + b"UNTOUCHED" * 1500
    raw = json.dumps(root, ensure_ascii=False, separators=(",", ":")).encode()
    pad = b"\0" * (-len(raw) % 4)
    header = struct.pack("<II", 4 + len(raw) + len(pad), len(raw)) + raw + pad
    path.write_bytes(struct.pack("<II", 4, len(header)) + header + data)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.app = self.directory / "app.asar"
        self.config = self.directory / "config.toml"
        fixture_archive(self.app)
        self.original_bytes = self.app.read_bytes()
        self.original_config = '# keep my comment\nmodel = "example-model"\n[desktop]\nsansFontSize = 15\nappearanceTheme = "system"\n[projects."/keep"]\ntrust_level = "trusted"\n'
        self.config.write_text(self.original_config)
        self.args = argparse.Namespace(app=self.app, config=self.config, state_dir=self.directory / "state")
        self.compatibility, self.css, self.theme = skin.assets()
        self.compatibility = {**self.compatibility, "asarSha256": skin.digest(self.app),
            "htmlSha256": hashlib.sha256(skin.Asar(self.app).read("webview/index.html")).hexdigest()}
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.temp.cleanup()

    def prepare(self):
        with skin.locked(self.args.state_dir):
            return skin.prepare(self.args, self.compatibility, self.css)

    def installed(self):
        state, already = self.prepare()
        skin.install(self.args, state, already, self.theme)
        return state

    def test_prepare_only_changes_state_directory(self):
        state, already = self.prepare()
        self.assertFalse(already)
        self.assertEqual(state["status"], "prepared")
        self.assertEqual(self.app.read_bytes(), self.original_bytes)
        self.assertEqual(self.config.read_text(), self.original_config)

    def test_archive_only_changes_html_entry_and_appends_payload(self):
        state, _ = self.prepare()
        old = skin.Asar(self.app)
        new = skin.Asar(self.args.state_dir / "patched.asar")
        self.assertEqual(old.base, new.base)
        old_root = json.loads(json.dumps(old.root))
        new_root = json.loads(json.dumps(new.root))
        del old_root["files"]["webview"]["files"]["index.html"]
        del new_root["files"]["webview"]["files"]["index.html"]
        self.assertEqual(old_root, new_root)
        self.assertEqual(self.original_bytes[old.base:], new.path.read_bytes()[new.base:len(self.original_bytes)])
        html = new.read("webview/index.html")
        self.assertEqual(html.count(skin.MARKER.encode()), 1)
        self.assertEqual(new.read("untouched.bin"), old.read("untouched.bin"))
        self.assertEqual(state["patchedSha256"], skin.digest(new.path))
        self.assertEqual(hashlib.sha256(html).hexdigest(), new.entry("webview/index.html")["integrity"]["hash"])

    def test_install_repeat_and_uninstall_preserve_other_configuration(self):
        self.installed()
        installed_bytes = self.app.read_bytes()
        _, _, config = skin.read_config(self.config)
        self.assertEqual(config["appearanceTheme"]["value"], "dark")
        self.assertEqual(config["appearanceDarkChromeTheme"]["value"]["fonts"], {})
        # A change made later by the user must survive repeated installation and uninstall.
        self.config.write_text(self.config.read_text().replace("sansFontSize = 15", "sansFontSize = 16"))
        state, already = self.prepare()
        self.assertTrue(already)
        skin.install(self.args, state, already, self.theme)
        self.assertEqual(self.app.read_bytes(), installed_bytes)
        skin.uninstall(self.args)
        self.assertEqual(self.app.read_bytes(), self.original_bytes)
        self.assertEqual(skin.toml_module().parse(self.config.read_text())["desktop"].unwrap(),
                         {"sansFontSize": 16, "appearanceTheme": "system"})
        self.assertIn("# keep my comment", self.config.read_text())
        self.assertIn('[projects."/keep"]', self.config.read_text())
        skin.uninstall(self.args)

    def test_uninstall_keeps_user_changed_appearance(self):
        self.installed()
        self.config.write_text(self.config.read_text().replace('appearanceTheme = "dark"', 'appearanceTheme = "light"'))
        skin.uninstall(self.args)
        self.assertEqual(skin.read_config(self.config)[2]["appearanceTheme"]["value"], "light")

    def test_install_preserves_custom_fonts(self):
        text, document, _ = skin.read_config(self.config)
        old = dict(self.theme["theme"])
        old["fonts"] = {"ui": "Custom Sans", "code": "Custom Mono", "content": "Custom Serif"}
        skin.write_config(self.config, text, document,
                          {"appearanceDarkChromeTheme": {"present": True, "value": old}})
        self.installed()
        fonts = skin.read_config(self.config)[2]["appearanceDarkChromeTheme"]["value"]["fonts"]
        self.assertEqual(fonts, old["fonts"])

    def test_unknown_version_and_modified_build_rejected(self):
        fixture_archive(self.app, "99.0.0")
        with self.assertRaisesRegex(skin.SkinError, "不支持应用版本"):
            skin.validate_original(self.app, self.compatibility)
        self.app.write_bytes(self.original_bytes + b"different-build")
        with self.assertRaisesRegex(skin.SkinError, "哈希"):
            skin.validate_original(self.app, self.compatibility)

    def test_upgraded_app_is_not_overwritten_on_uninstall(self):
        self.installed()
        self.app.write_bytes(self.app.read_bytes() + b"update")
        config = self.config.read_bytes()
        with self.assertRaisesRegex(skin.SkinError, "升级或改动"):
            skin.uninstall(self.args)
        self.assertEqual(self.config.read_bytes(), config)

    def test_configuration_failure_rolls_back_archive(self):
        state, already = self.prepare()
        with mock.patch.object(skin, "write_config", side_effect=skin.SkinError("injected failure")):
            with self.assertRaisesRegex(skin.SkinError, "injected failure"):
                skin.install(self.args, state, already, self.theme)
        self.assertEqual(self.app.read_bytes(), self.original_bytes)
        self.assertEqual(self.config.read_text(), self.original_config)

    def test_concurrent_config_write_is_not_overwritten(self):
        text, doc, _ = skin.read_config(self.config)
        self.config.write_text(self.original_config + "# external update\n")
        changes = {"appearanceTheme": {"present": True, "value": "dark"}}
        with self.assertRaisesRegex(skin.SkinError, "发生变化"):
            skin.write_config(self.config, text, doc, changes)
        self.assertIn("# external update", self.config.read_text())

    def test_interrupted_installation_resumes_configuration(self):
        state, _ = self.prepare()
        state["status"] = "installing"
        state["previousSettings"] = skin.read_config(self.config)[2]
        state["installedSettings"] = {k: {"present": True, "value": v}
                                      for k, v in skin.desired_settings(self.theme).items()}
        skin.save_state(self.args.state_dir, state)
        self.app.write_bytes((self.args.state_dir / "patched.asar").read_bytes())
        state, already = self.prepare()
        skin.install(self.args, state, already, self.theme)
        self.assertEqual(skin.read_config(self.config)[2]["appearanceTheme"]["value"], "dark")
        skin.uninstall(self.args)
        self.assertEqual(skin.read_config(self.config)[2]["appearanceTheme"]["value"], "system")

    def test_corrupt_backup_prevents_uninstall(self):
        self.installed()
        (self.args.state_dir / "original.asar").write_bytes(b"corrupt")
        before = self.app.read_bytes()
        with self.assertRaisesRegex(skin.SkinError, "备份校验失败"):
            skin.uninstall(self.args)
        self.assertEqual(self.app.read_bytes(), before)

    def revision(self):
        css = self.css + "\n/* updated revision fixture */\n"
        return {**self.compatibility, "skinVersion": "1.0.2",
                "cssSha256": hashlib.sha256(css.encode()).hexdigest()}, css

    def test_sidebar_decorations_are_removed(self):
        for forbidden in ("data-sidebar-header-search", "linear-gradient", "background-position", "content:"):
            self.assertNotIn(forbidden, self.css)

    def test_revision_staging_does_not_change_installed_resources_or_state(self):
        self.installed()
        before = self.app.read_bytes(), self.config.read_bytes(), (self.args.state_dir / "state.json").read_bytes()
        compatibility, css = self.revision()
        pending = skin.prepare_update(self.args, compatibility, css)
        self.assertEqual(before, (self.app.read_bytes(), self.config.read_bytes(),
                                 (self.args.state_dir / "state.json").read_bytes()))
        self.assertTrue((self.args.state_dir / pending["patchedFile"]).exists())

    def test_update_keeps_original_uninstall_record_and_user_settings(self):
        self.installed()
        original_state = skin.load_json(self.args.state_dir / "state.json")
        self.config.write_text(self.config.read_text().replace("sansFontSize = 15", "sansFontSize = 16"))
        config = self.config.read_bytes()
        compatibility, css = self.revision()
        skin.update(self.args, compatibility, css)
        updated = skin.load_json(self.args.state_dir / "state.json")
        self.assertEqual(updated["previousSettings"], original_state["previousSettings"])
        self.assertEqual(updated["installedSettings"], original_state["installedSettings"])
        self.assertEqual(self.config.read_bytes(), config)
        self.assertEqual(skin.digest(self.app), updated["patchedSha256"])
        self.assertIn(css.encode(), skin.Asar(self.app).read("webview/index.html"))
        self.assertIsNone(skin.prepare_update(self.args, compatibility, css))
        skin.uninstall(self.args)
        self.assertEqual(self.app.read_bytes(), self.original_bytes)
        self.assertIn("sansFontSize = 16", self.config.read_text())
        self.assertEqual(skin.read_config(self.config)[2]["appearanceTheme"]["value"], "system")

    def test_failed_revision_commit_restores_previous_skin(self):
        self.installed()
        before = self.app.read_bytes(), (self.args.state_dir / "state.json").read_bytes()
        compatibility, css = self.revision()
        skin.prepare_update(self.args, compatibility, css)
        original_save = skin.save_state
        def save(directory, state, name="state.json"):
            if name == "state.json" and state["cssSha256"] == compatibility["cssSha256"]:
                raise skin.SkinError("injected update commit failure")
            original_save(directory, state, name)
        with mock.patch.object(skin, "save_state", side_effect=save):
            with self.assertRaisesRegex(skin.SkinError, "injected update commit"):
                skin.update(self.args, compatibility, css)
        self.assertEqual(self.app.read_bytes(), before[0])
        self.assertEqual((self.args.state_dir / "state.json").read_bytes(), before[1])

    def test_interrupted_revision_update_recovers(self):
        self.installed()
        compatibility, css = self.revision()
        pending = skin.prepare_update(self.args, compatibility, css)
        self.app.write_bytes((self.args.state_dir / pending["patchedFile"]).read_bytes())
        skin.update(self.args, compatibility, css)
        state = skin.load_json(self.args.state_dir / "state.json")
        self.assertEqual(state["cssSha256"], compatibility["cssSha256"])
        self.assertFalse((self.args.state_dir / "update.json").exists())
        skin.uninstall(self.args)
        self.assertEqual(self.app.read_bytes(), self.original_bytes)

    def test_moved_backup_directory_can_update_and_uninstall(self):
        self.installed()
        moved = self.directory / "moved-project/.state"
        moved.parent.mkdir()
        shutil.move(str(self.args.state_dir), moved)
        self.args.state_dir = moved
        compatibility, css = self.revision()
        skin.update(self.args, compatibility, css)
        skin.uninstall(self.args)
        self.assertEqual(self.app.read_bytes(), self.original_bytes)

    def test_update_rejects_unknown_resource_changes(self):
        self.installed()
        compatibility, css = self.revision()
        self.app.write_bytes(self.app.read_bytes() + b"unknown")
        before = self.app.read_bytes()
        with self.assertRaisesRegex(skin.SkinError, "未知修改"):
            skin.prepare_update(self.args, compatibility, css)
        self.assertEqual(self.app.read_bytes(), before)

    def test_palette_contrast(self):
        def lum(hexcolor):
            channels = [int(hexcolor[i:i+2], 16) / 255 for i in (1, 3, 5)]
            linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
            return sum(a * b for a, b in zip(linear, (.2126, .7152, .0722)))
        def ratio(a, b):
            x, y = sorted((lum(a), lum(b)))
            return (y + .05) / (x + .05)
        for surface in ("#10171b", "#172126", "#1c2a30", "#1a3035"):
            for text in ("#e2eef0", "#9aafb5"):
                self.assertGreaterEqual(ratio(text, surface), 4.5, (text, surface))
        self.assertGreaterEqual(ratio("#0c2427", "#39c5bb"), 4.5)
        self.assertGreaterEqual(ratio("#6cd6a4", "#18382e"), 4.5)
        self.assertGreaterEqual(ratio("#f08080", "#3b252c"), 4.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
