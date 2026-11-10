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
        """What main() does before launching, without launching."""
        m = self.load()
        m.ensure_dir(m.DOWNLOAD_DIR)
        chrome_exe = m.EXTRACT_DIR / "chrome.exe"
        if not chrome_exe.exists() and m.OFFER_FALLBACK:
            m.choose_first_build(m.load_state())
        m.update_to_latest()

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
        with self.assertUpdateError():
            self.start()
        self.assertEqual(self.picks, [])

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
        # Chrome for Testing still says 157, but ungoogled-chromium is out as 158
        # and Hibbiki as 159: Marmaduke 156 is then really 3 behind.
        self.net.add(HIBBIKI, "159.0.8144.1", "chrome.7z")
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
        self.assertEqual(stable, 159)
        spec = m.picker_spec("fallback", candidates, stable, original)
        statuses = {o["release"].repo: (o["status"], o["enabled"]) for o in spec["options"]}
        self.assertEqual(statuses[UNGOOGLED], ("2 versions behind", False))
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
        with self.assertUpdateError():
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


if __name__ == "__main__":
    unittest.main()
