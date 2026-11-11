"""Tests for the fallback builds. Windows only, like Chromium.py itself.

Run from the repo folder: python -m unittest discover tests

Every test loads a fresh copy of Chromium.py in a temporary folder, with
GitHub, Chrome for Testing and the downloads faked. Nothing goes online.
"""

import hashlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "Chromium.py"

MARMADUKE = "macchrome/winchrome"
UNGOOGLED = "ungoogled-software/ungoogled-chromium-windows"
HIBBIKI = "Hibbiki/chromium-win64"
SUPERMIUM = "win32ss/supermium"


def make_archive(widevine: bool = False, nesting: int = 1, marker: str = "") -> bytes:
    """A zip that looks like a Chromium build."""
    buf = io.BytesIO()
    prefix = "".join(f"level{i}/" for i in range(nesting))
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(prefix + "chrome.exe", "exe " + marker)
        zf.writestr(prefix + "chrome.dll", "dll")
        if widevine:
            zf.writestr(prefix + "WidevineCdm/manifest.json", "{}")
    return buf.getvalue()


class FakeNet:
    def __init__(self, stable: int) -> None:
        self.stable = stable
        self.releases: dict[str, dict] = {}
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.downloads: list[str] = []
        self.down: set[str] = set()

    def add(self, repo: str, version: str, asset_name: str, data: bytes | None = None,
            tag: str | None = None, digest_ok: bool = True) -> None:
        data = data if data is not None else make_archive(marker=f"{repo} {version}")
        url = f"https://example.invalid/{repo}/{asset_name}"
        digest = "sha256:" + hashlib.sha256(data if digest_ok else b"x").hexdigest()
        self.files[url] = data
        self.releases[repo.lower()] = {
            "tag_name": tag or version,
            "name": tag or version,
            "assets": [
                {"name": "mini_installer.exe", "browser_download_url": url + ".exe", "size": 1},
                {"name": asset_name, "browser_download_url": url, "size": len(data), "digest": digest},
            ],
        }

    def get_json(self, url: str) -> dict:
        self.calls.append(url)
        if "chrome-for-testing" in url:
            if "stable" in self.down:
                raise OSError("offline")
            return {"channels": {"Stable": {"version": f"{self.stable}.0.7000.10"}}}
        repo = url.split("/repos/", 1)[1].rsplit("/releases/latest", 1)[0].lower()
        if repo in self.down:
            raise OSError("offline")
        return self.releases[repo]

    def download(self, url: str, dest: Path) -> None:
        self.downloads.append(url)
        if url in self.down:
            raise OSError("download interrupted")
        dest.write_bytes(self.files[url])

    def github_calls(self) -> int:
        return sum("api.github.com" in c for c in self.calls)


class FallbackTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="cpu-test-"))
        shutil.copy(SCRIPT, self.dir / "Chromium.py")
        self.net = FakeNet(stable=157)
        self.picks: list[dict] = []
        self.answer: object = None
        self.load()

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def load(self):
        """(Re)load the script, as a new start of the launcher does."""
        name = f"chromium_under_test_{id(self)}_{len(self.picks)}_{len(self.net.calls)}"
        spec = importlib.util.spec_from_file_location(name, self.dir / "Chromium.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.http_get_json = self.net.get_json
        module.download_file = self.net.download
        module.is_chromium_running = lambda exe: False
        module.console = lambda *a, **k: None

        def fake_pick(spec):
            self.picks.append(spec)
            if callable(self.answer):
                return self.answer(spec)
            return self.answer

        module.pick_with_window = fake_pick
        self.m = module
        return module

    def start(self) -> None:
        """A plain start of the launcher: the real main(), minus the launch."""
        m = self.load()
        self.launched = False

        def launch(exe, args):
            self.launched = True

        m.launch_chromium = launch
        argv = sys.argv
        sys.argv = ["Chromium.py"]
        try:
            m.main()
        finally:
            sys.argv = argv

    def assertUpdateError(self):
        """The module is reloaded on every start, so match UpdateError by name."""
        test = self

        class Check:
            def __enter__(self):
                return self

            def __exit__(self, kind, value, tb):
                test.assertIsNotNone(kind, "UpdateError not raised")
                test.assertEqual(kind.__name__, "UpdateError")
                return True

        return Check()

    def installed(self) -> dict:
        return json.loads((self.dir / "latest.txt").read_text(encoding="utf-8"))

    def pick_repo(self, repo: str):
        def answer(spec):
            for i, option in enumerate(spec["options"]):
                if option["release"].repo.lower() == repo.lower():
                    return i
            raise AssertionError(f"{repo} not offered")
        return answer

    def default_builds(self, marmaduke: str = "156.0.8078.84") -> None:
        self.net.add(MARMADUKE, marmaduke, f"ungoogled-chromium-{marmaduke}-1_Win64.7z",
                     data=make_archive(widevine=True, marker=f"marmaduke {marmaduke}"),
                     tag=f"v{marmaduke}-M{marmaduke}-r1706539-Win64")
        self.net.add(UNGOOGLED, "157.0.8103.38", "ungoogled-chromium_157.0.8103.38-1.1_windows_x64.zip",
                     tag="157.0.8103.38-1.1")
        self.net.add(HIBBIKI, "157.0.8103.39", "chrome.7z",
                     data=make_archive(nesting=2, widevine=True), tag="v157.0.8103.39-r1717349")
        self.net.add(SUPERMIUM, "150.0.7871.255", "supermium_150_64_nonsetup.zip",
                     tag="v150-r2.02")
        self.net.releases[SUPERMIUM.lower()]["name"] = "Release 150.0.7871.255 R2.02"

    def install_marmaduke(self) -> None:
        """A regular install from before, without the picker."""
        self.answer = self.pick_repo(MARMADUKE)
        self.start()
        self.picks.clear()


class PureFunctions(FallbackTestCase):
    def test_parse_version_from_real_tags(self):
        m = self.m
        self.assertEqual(m.parse_version("v156.8078.84-M156.0.8078.84-r1706539-Win64"), "156.0.8078.84")
        self.assertEqual(m.parse_version("157.0.8103.38-1.1"), "157.0.8103.38")
        self.assertEqual(m.parse_version("v157.0.8103.39-r1717349"), "157.0.8103.39")
        self.assertEqual(m.parse_version("v150-r2.02", "Release 150.0.7871.255 R2.02"), "150.0.7871.255")
        self.assertEqual(m.parse_version("ungoogled-chromium-156.0.8078.84-1_Win64.7z"), "156.0.8078.84")
        self.assertIsNone(m.parse_version("v150-r2.02", None, "supermium_150_64_nonsetup.zip"))

    def test_freshness(self):
        f = self.m.freshness
        self.assertEqual(f(157, 157), "current")
        self.assertEqual(f(158, 157), "current")
        self.assertEqual(f(156, 157), "behind")
        self.assertEqual(f(155, 157), "outdated")
        self.assertEqual(f(None, 157), "unknown")

    def test_warnings(self):
        m = self.m
        marmaduke = m.known_build(MARMADUKE)
        self.assertEqual(m.compare_builds(m.known_build(UNGOOGLED), marmaduke), ["Compared with Marmaduke, no Widevine"])
        self.assertEqual(
            m.compare_builds(m.known_build(SUPERMIUM), marmaduke),
            ["Less private than Marmaduke: Google sync", "Compared with Marmaduke, no HEVC, no Widevine"],
        )
        self.assertEqual(m.compare_builds(m.known_build(HIBBIKI), marmaduke), ["Less private than Marmaduke: Google sync"])

    def test_legacy_version_file(self):
        (self.dir / "latest.txt").write_text("ungoogled-chromium-156.0.8078.84-1_Win64.7z", encoding="utf-8")
        m = self.load()
        self.assertEqual(
            m.read_installed(),
            {"repo": MARMADUKE, "asset": "ungoogled-chromium-156.0.8078.84-1_Win64.7z", "version": "156.0.8078.84"},
        )


class FirstRun(FallbackTestCase):
    def test_picker_on_first_run_and_choice_saved(self):
        self.default_builds()
        self.answer = self.pick_repo(HIBBIKI)
        self.start()

        spec = self.picks[0]
        self.assertEqual(spec["headline"], "Choose a Chromium build")
        names = [o["release"].repo for o in spec["options"]]
        self.assertEqual(names, [MARMADUKE, UNGOOGLED, HIBBIKI, SUPERMIUM])
        self.assertEqual(spec["preselect"], 0)
        self.assertFalse(spec["options"][3]["enabled"], "Supermium 150 is outdated")
        self.assertEqual(spec["faq_url"], self.m.WIDEVINE_FAQ_URL)

        self.assertEqual(self.installed()["repo"], HIBBIKI)
        self.assertTrue((self.dir / "bin" / "chrome.exe").exists(), "two nested folders flattened")
        config = (self.dir / "config.jsonc").read_text(encoding="utf-8")
        self.assertIn('"repo": "Hibbiki/chromium-win64"', config)
        self.assertIn('"asset_suffix": "chrome.7z"', config)
        self.assertIn("// Supported placeholders", config, "comments kept")

        self.picks.clear()
        self.start()
        self.assertEqual(self.picks, [], "no picker once a build is installed")

    def test_closing_the_first_picker_keeps_the_configured_build(self):
        self.default_builds()
        self.answer = None
        self.start()
        self.assertEqual(self.installed()["repo"], MARMADUKE)

    def test_offline_first_run_skips_picker(self):
        self.default_builds()
        self.net.down |= {MARMADUKE.lower(), UNGOOGLED.lower(), HIBBIKI.lower(), SUPERMIUM.lower(), "stable"}
        with self.assertRaises(RuntimeError):
            self.start()
        self.assertEqual(self.picks, [])

    def test_one_picker_per_start_on_first_run(self):
        self.default_builds()
        self.net.stable = 158  # the default, Marmaduke 156, is outdated
        self.answer = None
        self.start()
        self.assertEqual(len(self.picks), 1)
        self.assertEqual(self.picks[0]["headline"], "Choose a Chromium build")

    def test_custom_repo_is_offered_too(self):
        self.default_builds()
        self.net.add("someone/own-chromium", "157.0.1.1", "build_Win64.7z")
        (self.dir / "config.jsonc").write_text(
            '{"repo": "someone/own-chromium", "asset_suffix": "_Win64.7z"}', encoding="utf-8"
        )
        self.answer = lambda spec: spec["preselect"]
        self.start()
        self.assertEqual(self.picks[0]["options"][-1]["release"].repo, "someone/own-chromium")
        self.assertEqual(self.installed()["repo"], "someone/own-chromium")

    def test_disabled_offer_fallback(self):
        self.default_builds()
        (self.dir / "config.jsonc").write_text('{"offer_fallback": false}', encoding="utf-8")
        self.net.stable = 160
        self.start()
        self.assertEqual(self.picks, [])
        self.assertEqual(self.installed()["repo"], MARMADUKE)


class Fallback(FallbackTestCase):
    def setUp(self):
        super().setUp()
        self.default_builds()
        self.install_marmaduke()

    def make_marmaduke_outdated(self):
        self.net.stable = 158  # Marmaduke 156 is now 2 behind
        (self.dir / "fallback.json").unlink(missing_ok=True)  # drop the cached stable version

    def test_current_original_needs_no_picker(self):
        self.start()
        self.assertEqual(self.picks, [])
        self.assertEqual(self.installed()["repo"], MARMADUKE)

    def test_switch_to_fallback_and_back(self):
        self.make_marmaduke_outdated()
        self.net.add(UNGOOGLED, "158.0.8127.20", "ungoogled-chromium_158.0.8127.20-1.1_windows_x64.zip")
        self.answer = self.pick_repo(UNGOOGLED)
        self.start()

        spec = self.picks[0]
        self.assertEqual(spec["headline"], "Marmaduke is behind Chrome stable")
        self.assertIn("2 major versions behind Chrome 158", spec["intro"])
        self.assertNotIn(MARMADUKE, [o["release"].repo for o in spec["options"]])
        self.assertEqual(spec["options"][spec["preselect"]]["release"].repo, UNGOOGLED)
        self.assertEqual(spec["options"][0]["warnings"], ["Compared with Marmaduke, no Widevine"])
        self.assertEqual(self.installed()["repo"], UNGOOGLED)

        # Next start: Marmaduke still behind, the fallback is kept, no picker,
        # and only the two repos are asked.
        self.picks.clear()
        before = self.net.github_calls()
        self.start()
        self.assertEqual(self.picks, [])
        self.assertEqual(self.net.github_calls() - before, 2)
        self.assertEqual(self.installed()["repo"], UNGOOGLED)

        # Marmaduke catches up: back to it.
        self.net.add(MARMADUKE, "158.0.8127.10", "ungoogled-chromium-158.0.8127.10-1_Win64.7z",
                     data=make_archive(widevine=True))
        self.start()
        self.assertEqual(self.installed()["repo"], MARMADUKE)
        self.assertEqual(self.installed()["version"], "158.0.8127.10")

    def test_stable_from_github_when_chrome_for_testing_lags(self):
        # Chrome for Testing still says 157, but Hibbiki is already out as 158:
        # Marmaduke 156 is then really 2 behind.
        self.net.add(HIBBIKI, "158.0.8144.1", "chrome.7z")
        self.answer = None
        (self.dir / "fallback.json").unlink()
        self.start()
        # Marmaduke is 1 behind 157 only, so no scan and no picker happen.
        self.assertEqual(self.picks, [])

        # Once the fallback is used, its own release raises "stable".
        m = self.m
        state = m.load_state()
        original = m.fetch_release(MARMADUKE, "_Win64.7z")
        candidates = m.scan_builds(state)
        stable = m.effective_stable(state, [original.major, *[r.major for r in candidates]])
        self.assertEqual(stable, 158)
        spec = m.picker_spec("fallback", candidates, stable, original)
        statuses = {o["release"].repo: (o["status"], o["enabled"]) for o in spec["options"]}
        self.assertEqual(statuses[UNGOOGLED], ("1 version behind", True))
        self.assertEqual(statuses[HIBBIKI], ("up to date", True))

    def test_decline_is_remembered_until_the_next_chrome_version(self):
        self.make_marmaduke_outdated()
        self.answer = None
        self.start()
        self.assertEqual(len(self.picks), 1)
        self.assertEqual(self.installed()["repo"], MARMADUKE)

        self.start()
        self.assertEqual(len(self.picks), 1, "not asked again for the same Chrome version")

        self.net.stable = 159
        self.net.add(HIBBIKI, "159.0.8144.1", "chrome.7z")
        (self.dir / "fallback.json").write_text(
            json.dumps({"declined_for_stable": 158}), encoding="utf-8"
        )
        self.start()
        self.assertEqual(len(self.picks), 2, "asked again for a new Chrome version")

    def test_fallback_unreachable_keeps_installed(self):
        self.make_marmaduke_outdated()
        self.answer = self.pick_repo(UNGOOGLED)
        self.start()
        self.net.down.add(UNGOOGLED.lower())
        self.picks.clear()
        self.start()
        self.assertEqual(self.picks, [])
        self.assertEqual(self.installed()["repo"], UNGOOGLED)

    def test_config_change_is_not_taken_for_a_fallback(self):
        self.m.set_tracked_build(self.m.Release(HIBBIKI, {}, None))
        self.make_marmaduke_outdated()
        self.start()
        self.assertEqual(self.picks, [])
        self.assertEqual(self.installed()["repo"], HIBBIKI)
        self.assertEqual(self.installed()["tracked"], HIBBIKI)

    def test_decline_holds_when_the_scan_raises_stable(self):
        # Chrome for Testing says 158, Hibbiki is already out as 159.
        self.make_marmaduke_outdated()
        self.net.add(HIBBIKI, "159.0.8144.1", "chrome.7z")
        self.answer = None
        self.start()
        self.assertEqual(len(self.picks), 1)
        self.assertIn("Chrome 159", self.picks[0]["intro"])
        self.start()
        self.assertEqual(len(self.picks), 1)

    def test_fallback_unreachable_and_bin_gone(self):
        self.make_marmaduke_outdated()
        self.answer = self.pick_repo(UNGOOGLED)
        self.start()
        shutil.rmtree(self.dir / "bin")
        self.net.down.add(UNGOOGLED.lower())
        self.answer = None
        with self.assertUpdateError():
            self.m.update_to_latest()

    def test_failed_scan_is_not_taken_as_a_decline(self):
        self.make_marmaduke_outdated()
        self.net.down |= {UNGOOGLED.lower(), HIBBIKI.lower(), SUPERMIUM.lower()}
        self.start()
        self.assertEqual(self.picks, [])
        self.assertNotIn("declined_for_stable", json.loads((self.dir / "fallback.json").read_text()))
        self.assertTrue(self.launched)

        self.net.down.clear()
        state = json.loads((self.dir / "fallback.json").read_text())
        state.pop("scan", None)
        (self.dir / "fallback.json").write_text(json.dumps(state), encoding="utf-8")
        self.start()
        self.assertEqual(len(self.picks), 1, "offered once the builds can be checked")

    def test_keep_on_an_outdated_fallback_returns_to_the_configured_build(self):
        self.make_marmaduke_outdated()
        self.answer = self.pick_repo(UNGOOGLED)
        self.start()
        self.assertEqual(self.installed()["repo"], UNGOOGLED)
        self.net.stable = 160  # now ungoogled-chromium 157 is outdated too
        (self.dir / "fallback.json").unlink()
        self.answer = None
        self.start()
        self.assertIn("Keep Marmaduke", self.picks[-1]["cancel_label"])
        self.assertEqual(self.installed()["repo"], MARMADUKE)

    def test_unfinished_fallback_install_is_resumed_without_asking(self):
        self.make_marmaduke_outdated()
        url = self.net.releases[UNGOOGLED.lower()]["assets"][1]["browser_download_url"]
        self.net.down.add(url)
        self.answer = self.pick_repo(UNGOOGLED)
        self.start()
        self.assertEqual(self.installed()["repo"], MARMADUKE, "download failed")
        self.assertTrue(self.launched, "the installed build still starts")

        self.net.down.clear()
        self.answer = None
        self.start()
        self.assertEqual(len(self.picks), 1, "not asked again")
        self.assertEqual(self.installed()["repo"], UNGOOGLED)

    def test_unexpected_error_still_launches(self):
        m = self.load()

        def broken(*args):
            raise OSError("archive locked by antivirus")

        self.net.add(MARMADUKE, "157.0.1.1", "x-157.0.1.1_Win64.7z")
        m.launch_chromium = lambda exe, args: setattr(self, "launched", True)
        m.install = broken
        self.launched = False
        argv = sys.argv
        sys.argv = ["Chromium.py"]
        try:
            m.main()
        finally:
            sys.argv = argv
        self.assertTrue(self.launched)

    def test_misread_version_does_not_raise_stable(self):
        self.net.add(HIBBIKI, "2025.11.10.1", "chrome.7z", tag="build-2025.11.10.1")
        m = self.m
        state = m.load_state()
        scanned = [r.major for r in m.scan_builds({})]
        self.assertIn(2025, scanned)
        self.assertEqual(m.effective_stable(state, scanned), 157)

    def test_outdated_fallback_asks_again(self):
        self.make_marmaduke_outdated()
        self.answer = self.pick_repo(UNGOOGLED)
        self.start()
        self.net.stable = 160
        self.net.add(HIBBIKI, "160.0.8200.1", "chrome.7z")
        (self.dir / "fallback.json").unlink()
        self.answer = self.pick_repo(HIBBIKI)
        self.picks.clear()
        self.start()
        self.assertEqual(len(self.picks), 1)
        self.assertEqual(self.installed()["repo"], HIBBIKI)

    def test_scan_and_stable_are_cached_for_an_hour(self):
        self.make_marmaduke_outdated()
        self.answer = None
        self.start()
        stable_calls = sum("chrome-for-testing" in c for c in self.net.calls)
        (self.dir / "fallback.json").write_text(
            json.dumps({**json.loads((self.dir / "fallback.json").read_text()), "declined_for_stable": None}),
            encoding="utf-8",
        )
        before = self.net.github_calls()
        self.start()
        self.assertEqual(sum("chrome-for-testing" in c for c in self.net.calls), stable_calls)
        self.assertEqual(self.net.github_calls() - before, 1, "only Marmaduke, the scan came from the cache")


class Widevine(FallbackTestCase):
    def setUp(self):
        super().setUp()
        self.default_builds()
        self.answer = self.pick_repo(UNGOOGLED)
        self.start()
        self.wv = self.dir / "bin" / "WidevineCdm"
        self.wv.mkdir()
        (self.wv / "manifest.json").write_text("mine", encoding="utf-8")

    def test_user_widevine_survives_an_update(self):
        self.assertFalse(self.installed()["bundled_widevine"])
        self.net.add(UNGOOGLED, "157.0.8103.50", "ungoogled-chromium_157.0.8103.50-1.1_windows_x64.zip")
        self.start()
        self.assertEqual(self.installed()["version"], "157.0.8103.50")
        self.assertEqual((self.wv / "manifest.json").read_text(encoding="utf-8"), "mine")

    def test_unknown_origin_widevine_is_kept(self):
        (self.dir / "latest.txt").unlink()
        self.net.add(UNGOOGLED, "157.0.8103.60", "ungoogled-chromium_157.0.8103.60-1.1_windows_x64.zip")
        self.start()
        self.assertEqual((self.wv / "manifest.json").read_text(encoding="utf-8"), "mine")

    def test_bundled_widevine_wins_and_yours_is_parked(self):
        self.m.set_tracked_build(self.m.Release(MARMADUKE, {}, None))
        self.start()
        self.assertEqual(self.installed()["repo"], MARMADUKE)
        self.assertEqual((self.wv / "manifest.json").read_text(encoding="utf-8"), "{}")
        self.assertEqual((self.dir / "WidevineCdm.kept" / "manifest.json").read_text(encoding="utf-8"), "mine")

    def test_bundled_widevine_is_not_carried_to_a_build_without_it(self):
        self.m.set_tracked_build(self.m.Release(MARMADUKE, {}, None))
        self.start()
        shutil.rmtree(self.dir / "WidevineCdm.kept")
        self.m.set_tracked_build(self.m.Release(UNGOOGLED, {}, None))
        self.net.add(UNGOOGLED, "157.0.8103.51", "ungoogled-chromium_157.0.8103.51-1.1_windows_x64.zip")
        self.start()
        self.assertEqual(self.installed()["repo"], UNGOOGLED)
        self.assertFalse(self.wv.exists(), "Marmaduke's own Widevine stays behind")


class Checksums(FallbackTestCase):
    def test_bad_download_is_rejected(self):
        self.default_builds()
        self.net.add(MARMADUKE, "157.0.1.1", "x_Win64.7z", digest_ok=False)
        self.answer = self.pick_repo(MARMADUKE)
        with self.assertRaises(RuntimeError):
            self.start()
        self.assertFalse((self.dir / "download" / "x_Win64.7z").exists())
        self.assertFalse((self.dir / "latest.txt").exists())

    def test_damaged_archive_on_disk_is_downloaded_again(self):
        self.default_builds()
        (self.dir / "download").mkdir()
        (self.dir / "download" / "ungoogled-chromium-156.0.8078.84-1_Win64.7z").write_bytes(b"broken")
        self.answer = self.pick_repo(MARMADUKE)
        self.start()
        self.assertEqual(self.installed()["repo"], MARMADUKE)


class ArchiveNames(FallbackTestCase):
    def test_hibbiki_updates_although_the_file_name_stays(self):
        self.default_builds()
        self.answer = self.pick_repo(HIBBIKI)
        self.start()
        self.assertEqual(self.installed()["version"], "157.0.8103.39")
        self.assertTrue((self.dir / "download" / "157.0.8103.39_chrome.7z").exists())

        self.net.add(HIBBIKI, "157.0.8103.50", "chrome.7z", data=make_archive(marker="new"),
                     tag="v157.0.8103.50-r1717400")
        self.start()
        self.assertEqual(self.installed()["version"], "157.0.8103.50")
        self.assertIn("new", (self.dir / "bin" / "chrome.exe").read_text())
        self.assertEqual(sorted(p.name for p in (self.dir / "download").iterdir()),
                         ["157.0.8103.50_chrome.7z"])

    def test_old_version_file_with_a_generic_name_reinstalls_once(self):
        self.default_builds()
        (self.dir / "config.jsonc").write_text(
            '{"repo": "Hibbiki/chromium-win64", "asset_suffix": "chrome.7z"}', encoding="utf-8")
        (self.dir / "bin").mkdir()
        (self.dir / "bin" / "chrome.exe").write_text("old")
        (self.dir / "latest.txt").write_text("chrome.7z", encoding="utf-8")
        self.start()
        self.assertEqual(self.installed()["version"], "157.0.8103.39")
        self.assertEqual(len(self.net.downloads), 1)
        self.start()  # and then it is up to date
        self.assertEqual(len(self.net.downloads), 1)

    def test_supermium_layout(self):
        self.default_builds()
        self.net.add(SUPERMIUM, "158.0.1.1", "supermium_158_64_nonsetup.zip", data=self._supermium_zip())
        self.answer = self.pick_repo(SUPERMIUM)
        self.start()
        bin_dir = self.dir / "bin"
        self.assertTrue((bin_dir / "chrome.exe").exists())
        self.assertFalse((bin_dir / "setup.exe").exists())

    @staticmethod
    def _supermium_zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("setup.exe", "installer")
            zf.writestr("NotoEmoji.ttf", "font")
            zf.writestr("Supermium/chrome.exe", "exe")
            zf.writestr("Supermium/150.0.7871.255/chrome.dll", "dll")
        return buf.getvalue()


class ConsolePicker(FallbackTestCase):
    def spec(self):
        m = self.m
        R = m.Release
        releases = [
            R(UNGOOGLED, {"name": "a"}, "157.0.8103.38"),
            R(HIBBIKI, {"name": "b"}, "157.0.8103.39"),
            R(SUPERMIUM, {"name": "c"}, "150.0.7871.255"),
        ]
        return m.picker_spec("fallback", releases, 158, R(MARMADUKE, {"name": "d"}, "156.0.8078.84"))

    def run_console(self, answers):
        import builtins
        answers = iter(answers)
        original = builtins.input
        builtins.input = lambda prompt="": next(answers)
        try:
            return self.m.pick_in_console(self.spec())
        finally:
            builtins.input = original

    def test_enter_takes_the_preselected_build(self):
        self.assertEqual(self.run_console([""]), 0)

    def test_outdated_and_invalid_answers_are_asked_again(self):
        self.assertEqual(self.run_console(["3", "x", "9", "2"]), 1)

    def test_n_declines(self):
        self.assertIsNone(self.run_console(["n"]))

    def test_window_failure_falls_back_to_console(self):
        m = self.m

        def broken(spec):
            raise RuntimeError("no display")

        m.pick_with_window = broken
        m.console_is_interactive = lambda: True
        m.pick_in_console = lambda spec: 1
        shown, choice = m.pick_build(self.spec())
        self.assertEqual((shown, choice.repo), (True, HIBBIKI))
        m.console_is_interactive = lambda: False
        self.assertEqual(m.pick_build(self.spec()), (False, None))


if __name__ == "__main__":
    unittest.main()
