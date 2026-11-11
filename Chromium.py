import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import winreg
import zipfile
from dataclasses import dataclass
from pathlib import Path
from string import Formatter


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.jsonc"

# Applied per socket operation, not to the transfer as a whole.
API_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 120

# One previous log is kept alongside the current one.
LOG_MAX_BYTES = 256 * 1024

DOWNLOAD_CHUNK = 256 * 1024

MB_OK = 0x00
MB_ICONERROR = 0x10


def console(message: str = "", end: str = "\n", stream=None) -> None:
    """Write to the console, if there is one.

    Started from a shortcut with pythonw.exe there is no console at all and
    the streams are None, which print() already treats as a no-op. Writing
    output must never be the reason a download or launch fails, so a broken
    or closed stream is ignored too.
    """
    try:
        print(message, end=end, file=stream, flush=True)
    except Exception:
        pass


def console_is_interactive() -> bool:
    """True for a real terminal, False when redirected or absent."""
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def write_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.replace(LOG_FILE.with_name(LOG_FILE.name + ".old"))
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{timestamp} {message}\n")
    except Exception:
        pass


def log(message: str) -> None:
    write_log(message)
    console(message)


def show_error(message: str, headline: str = "Failed to launch Chromium") -> None:
    """Report a fatal error.

    The launcher is normally started from a shortcut, where stderr goes
    nowhere, so a message box is the only thing the user will actually see.
    """
    write_log(f"ERROR: {message}")
    try:
        ctypes.windll.user32.MessageBoxW(
            0,
            f"{headline}.\n\n{message}",
            "Chromium-Py-Updater",
            MB_OK | MB_ICONERROR,
        )
    except Exception:
        pass
    console(f"{headline}: {message}", stream=sys.stderr)

CONFIG_COMMENT = """\
  // Supported placeholders:
  // {base_dir}     = script folder
  // {system_temp}  = Windows temp folder
  // {profile_dir}  = resolved profile path
  // {temp_dir}     = resolved temp path
  // {extract_dir}  = resolved bin path
  // {download_dir} = resolved download path
  //
  // Paths may be relative or absolute.
  // Relative paths are resolved against {base_dir}."""


def write_default_config(path: Path, config: dict) -> None:
    lines = json.dumps(config, indent=2).splitlines()
    path.write_text(
        "\n".join([lines[0], CONFIG_COMMENT, *lines[1:]]) + "\n",
        encoding="utf-8",
    )


def load_config() -> dict:
    default_config = {
        "repo": "macchrome/winchrome",
        "asset_suffix": "_Win64.7z",
        "download_dir": "download",
        "extract_dir": "bin",
        "profile_dir": "profile",
        "version_file": "latest.txt",
        "log_file": "launch.log",
        "temp_dir": "temp",
        "offer_fallback": True,
        "chromium_args": [
            "--user-data-dir={profile_dir}",
            "--no-default-browser-check",
            "--disable-logging",
            "--no-report-upload",
            "--disk-cache-dir={temp_dir}",
            "--crash-dumps-dir={temp_dir}",
            "--telemetry-extension-dir={temp_dir}",
        ],
    }

    if not CONFIG_FILE.exists():
        try:
            # The build is chosen in the picker, which adds repo and
            # asset_suffix; setting them by hand is for builds not in its list.
            write_default_config(
                CONFIG_FILE,
                {k: v for k, v in default_config.items() if k not in ("repo", "asset_suffix")},
            )
        except OSError:
            pass
        return default_config

    def load_jsonc(path: Path) -> dict:
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if stripped.startswith("//"):
                continue
            lines.append(line)
        return json.loads("\n".join(lines))

    user_config = load_jsonc(CONFIG_FILE)

    unknown = sorted(set(user_config) - set(default_config))
    if unknown:
        raise RuntimeError(
            f"Unknown setting(s) in {CONFIG_FILE.name}: {', '.join(unknown)}. "
            f"Known settings: {', '.join(sorted(default_config))}"
        )

    default_config.update(user_config)
    return default_config


def safe_format(template: str, values: dict[str, str], context: str) -> str:
    formatter = Formatter()
    field_names = {
        field_name
        for _, field_name, _, _ in formatter.parse(template)
        if field_name
    }

    unknown = sorted(name for name in field_names if name not in values)
    if unknown:
        allowed = ", ".join(sorted(values.keys()))
        unknown_str = ", ".join(unknown)
        raise RuntimeError(
            f"Unknown placeholder(s) in {context}: {unknown_str}. "
            f"Allowed placeholders: {allowed}"
        )

    try:
        return template.format(**values)
    except Exception as e:
        raise RuntimeError(f"Failed to format {context}: {template} ({e})") from e


try:
    CONFIG = load_config()

    REPO = CONFIG["repo"]
    ASSET_SUFFIX = CONFIG["asset_suffix"]
    OFFER_FALLBACK = bool(CONFIG["offer_fallback"])

    FORMAT_VALUES = {
        "base_dir": str(BASE_DIR),
        "system_temp": tempfile.gettempdir(),
    }

    DOWNLOAD_DIR = Path(safe_format(CONFIG["download_dir"], FORMAT_VALUES, "download_dir"))
    EXTRACT_DIR = Path(safe_format(CONFIG["extract_dir"], FORMAT_VALUES, "extract_dir"))
    PROFILE_DIR = Path(safe_format(CONFIG["profile_dir"], FORMAT_VALUES, "profile_dir"))
    VERSION_FILE = Path(safe_format(CONFIG["version_file"], FORMAT_VALUES, "version_file"))
    LOG_FILE = Path(safe_format(CONFIG["log_file"], FORMAT_VALUES, "log_file"))
    TEMP_CHROMIUM_DIR = Path(safe_format(CONFIG["temp_dir"], FORMAT_VALUES, "temp_dir"))

    if not DOWNLOAD_DIR.is_absolute():
        DOWNLOAD_DIR = BASE_DIR / DOWNLOAD_DIR
    if not EXTRACT_DIR.is_absolute():
        EXTRACT_DIR = BASE_DIR / EXTRACT_DIR
    if not PROFILE_DIR.is_absolute():
        PROFILE_DIR = BASE_DIR / PROFILE_DIR
    if not VERSION_FILE.is_absolute():
        VERSION_FILE = BASE_DIR / VERSION_FILE
    if not LOG_FILE.is_absolute():
        LOG_FILE = BASE_DIR / LOG_FILE
    if not TEMP_CHROMIUM_DIR.is_absolute():
        TEMP_CHROMIUM_DIR = BASE_DIR / TEMP_CHROMIUM_DIR

    CHROMIUM_ARGS = CONFIG["chromium_args"]

    # Cached Chrome stable version and build scan, plus a declined fallback.
    STATE_FILE = VERSION_FILE.with_name("fallback.json")
except Exception as startup_error:
    # Runs before main() exists to catch anything; without this a bad
    # config.jsonc is a silent traceback on a console-less launch.
    show_error(str(startup_error))
    sys.exit(1)


def http_get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "chromium-launcher",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=API_TIMEOUT) as response:
        return json.load(response)


def format_mib(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.1f}"


class DownloadProgress:
    """Console feedback for a download of a few hundred megabytes.

    Rewrites a single line on a terminal, prints a line every 10% when output
    is redirected, and stays silent when there is no console.
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self.interactive = console_is_interactive()
        self.end = "\r" if self.interactive else "\n"
        self.step = -1

    def update(self, done: int) -> None:
        if self.total <= 0:
            # No Content-Length: report every 16 MiB so it is visibly moving.
            step = done // (16 * 1024 * 1024)
            if step != self.step:
                self.step = step
                console(f"  {format_mib(done)} MiB", end=self.end)
            return

        percent = min(100, int(done * 100 / self.total))
        step = percent if self.interactive else percent // 10
        if step != self.step:
            self.step = step
            console(
                f"  {percent:3d}%  {format_mib(done)} / {format_mib(self.total)} MiB",
                end=self.end,
            )

    def finish(self) -> None:
        if self.interactive and self.step >= 0:
            console()


def download_file(url: str, dest: Path) -> None:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "chromium-launcher"},
    )
    # Downloaded beside the target and renamed on success, so an interrupted
    # transfer never leaves something that looks like a complete archive.
    part = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as response, part.open(
            "wb"
        ) as f:
            progress = DownloadProgress(int(response.headers.get("Content-Length") or 0))
            done = 0
            while True:
                chunk = response.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                progress.update(done)
            progress.finish()
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def find_asset(release: dict, suffix: str) -> dict | None:
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.endswith(suffix):
            return asset
    return None


def file_matches_digest(path: Path, digest: str | None) -> bool:
    """Check a file against GitHub's asset digest ("sha256:<hex>").

    Older releases carry no digest; those are accepted as they are.
    """
    if not digest or not digest.startswith("sha256:"):
        return True
    sha = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(DOWNLOAD_CHUNK * 4), b""):
            sha.update(chunk)
    return sha.hexdigest() == digest.split(":", 1)[1].lower()


def read_text_file(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def recreate_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def remove_old_archives(keep_name: str) -> None:
    for pattern in ("*.zip", "*.7z"):
        for archive in DOWNLOAD_DIR.glob(pattern):
            if archive.name != keep_name:
                archive.unlink(missing_ok=True)
    for part in DOWNLOAD_DIR.glob("*.part"):
        part.unlink(missing_ok=True)


GENERIC_WRITE = 0x40000000
FILE_SHARE_ALL = 0x07
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_SHARING_VIOLATION = 32


def is_chromium_running(chrome_exe: Path) -> bool:
    # Windows refuses write access to the program file of a running process,
    # so this is instant and only matches our own build. Nothing is written.
    # Called directly because Python reports a read-only file the same way.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    handle = kernel32.CreateFileW(
        str(chrome_exe), GENERIC_WRITE, FILE_SHARE_ALL, None, OPEN_EXISTING, 0, None
    )
    if handle == INVALID_HANDLE_VALUE:
        return ctypes.get_last_error() == ERROR_SHARING_VIOLATION
    kernel32.CloseHandle(handle)
    return False


SYSTEM_TAR = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"


def find_7zip() -> Path | None:
    found = shutil.which("7z")
    if found:
        return Path(found)
    for env_var in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env_var)
        if root and (Path(root) / "7-Zip" / "7z.exe").exists():
            return Path(root) / "7-Zip" / "7z.exe"
    return None


def run_extractor(args: list[str]) -> None:
    # CREATE_NO_WINDOW: started with pythonw.exe, a console tool would
    # otherwise flash up its own window.
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(output[-1] if output else f"exit code {result.returncode}")


def extract_archive(archive: Path, dest: Path) -> None:
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(dest)
        return

    # Python cannot read 7z on its own. Windows 11 ships a tar that can;
    # Windows 10's cannot, so an installed 7-Zip is the fallback there.
    try:
        run_extractor([str(SYSTEM_TAR), "-xf", str(archive), "-C", str(dest)])
        return
    except Exception as e:
        tar_error = e

    sevenzip = find_7zip()
    if not sevenzip:
        raise RuntimeError(
            f"Windows could not unpack it ({tar_error}) and 7-Zip was not found. "
            "Install 7-Zip from https://www.7-zip.org or choose a release "
            "that comes as a .zip file"
        )

    log(f"Windows tar failed ({tar_error}), using 7-Zip")
    recreate_dir(dest)
    run_extractor([str(sevenzip), "x", "-y", f"-o{dest}", str(archive)])


def find_build_root(path: Path) -> Path:
    """The folder of the shallowest chrome.exe, a few levels deep at most.

    Most archives wrap the build in one folder; some nest it deeper or put
    extras beside it (Supermium ships setup.exe and a font next to its
    folder). Without any chrome.exe, single top-level folders are stepped
    into as before.
    """
    for depth in range(4):
        found = sorted(path.glob("/".join(["*"] * depth + ["chrome.exe"])))
        if found:
            return found[0].parent
    while True:
        items = list(path.iterdir())
        if len(items) != 1 or not items[0].is_dir():
            return path
        path = items[0]


def extract_flat(archive: Path, extract_dir: Path) -> None:
    """Replace extract_dir with the build from archive."""
    with tempfile.TemporaryDirectory(dir=BASE_DIR) as tmp_dir:
        tmp_path = Path(tmp_dir)
        unpacked = tmp_path / "unpacked"
        unpacked.mkdir()

        log("Extracting archive")
        extract_archive(archive, unpacked)

        root = find_build_root(unpacked)
        if root != unpacked:
            log("Flattening top-level folder")
        else:
            log("Moving extracted contents")

        recreate_dir(extract_dir)

        for item in list(root.iterdir()):
            shutil.move(str(item), str(extract_dir / item.name))


def build_chromium_args(extra_args: list[str]) -> list[str]:
    ensure_dir(TEMP_CHROMIUM_DIR)

    format_values = {
        "profile_dir": str(PROFILE_DIR),
        "temp_dir": str(TEMP_CHROMIUM_DIR),
        "base_dir": str(BASE_DIR),
        "extract_dir": str(EXTRACT_DIR),
        "download_dir": str(DOWNLOAD_DIR),
        "system_temp": tempfile.gettempdir(),
    }

    args = [
        safe_format(arg, format_values, "chromium_args entry")
        for arg in CHROMIUM_ARGS
    ]
    args.extend(extra_args)
    return args


def launch_chromium(chrome_exe: Path, extra_args: list[str]) -> None:
    args = [str(chrome_exe)]
    args.extend(build_chromium_args(extra_args))

    log("Starting Chromium")
    subprocess.Popen(args, cwd=chrome_exe.parent)


class UpdateError(Exception):
    """An update step failed, but an already-installed build may still run."""


# ---------------------------------------------------------------- known builds

# Windows x64 builds from the stable channel with one portable archive per
# release, i.e. no CPU-specific variants. What a build contains can't be
# detected before downloading it, so it is kept here by hand.
#
# Based on src/sources.ts of chromiumbuilds, https://github.com/AmirAgassi/chromiumbuilds
# Copyright (c) 2026 Amir Agassi, MIT License.
# ungoogled-chromium's codecs were tested by us: chromiumbuilds lists them as
# missing (https://github.com/AmirAgassi/chromiumbuilds/issues/11).
#
# google: "ungoogled" (no Google services), "nosync" (no Google sync) or "sync".
KNOWN_BUILDS = [
    {
        "name": "Marmaduke",
        "repo": "macchrome/winchrome",
        "asset_suffix": "_Win64.7z",
        "google": "ungoogled",
        "codecs": True,
        "hevc": True,
        "widevine": True,
    },
    {
        "name": "ungoogled-chromium",
        "repo": "ungoogled-software/ungoogled-chromium-windows",
        "asset_suffix": "_windows_x64.zip",
        "google": "ungoogled",
        "codecs": True,
        "hevc": True,
        "widevine": False,
        "widevine_by_hand": True,
    },
    {
        "name": "Hibbiki",
        "repo": "Hibbiki/chromium-win64",
        "asset_suffix": "chrome.7z",
        "google": "sync",
        "codecs": True,
        "hevc": True,
        "widevine": True,
    },
    {
        "name": "Supermium",
        "repo": "win32ss/supermium",
        "asset_suffix": "_64_nonsetup.zip",
        "google": "sync",
        "codecs": True,
        "hevc": False,
        "widevine": False,
        "note": "Made for older Windows versions (7, 8, 8.1)",
    },
]

PRIVACY_RANK = {"ungoogled": 0, "nosync": 1, "sync": 2}
GOOGLE_LABELS = {
    "ungoogled": "Ungoogled",
    "nosync": "No Google sync",
    "sync": "Google sync",
}
FEATURES = (
    ("codecs", "common codecs (H.264, AAC)"),
    ("hevc", "HEVC"),
    ("widevine", "Widevine"),
)

CHROME_STABLE_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json"
)
CACHE_SECONDS = 3600
# Two or more major versions behind Chrome stable means missing security fixes.
OUTDATED_GAP = 2

VERSION_RE = re.compile(r"(?<![\d.])(\d+)\.\d+\.\d+\.\d+")


def same_repo(a: str | None, b: str | None) -> bool:
    return (a or "").lower() == (b or "").lower()


def known_build(repo: str | None) -> dict | None:
    for build in KNOWN_BUILDS:
        if same_repo(build["repo"], repo):
            return build
    return None


def display_name(repo: str) -> str:
    build = known_build(repo)
    return build["name"] if build else repo


def parse_version(*texts: str | None) -> str | None:
    """The first a.b.c.d version in the given texts, e.g. a tag or file name."""
    for text in texts:
        match = VERSION_RE.search(text or "")
        if match:
            return match.group(0)
    return None


def major_of(version: str | None) -> int | None:
    return int(version.split(".")[0]) if version else None


@dataclass
class Release:
    repo: str
    asset: dict
    version: str | None

    @property
    def name(self) -> str:
        return self.asset["name"]

    @property
    def major(self) -> int | None:
        return major_of(self.version)

    def to_json(self) -> dict:
        keep = ("name", "browser_download_url", "size", "digest")
        return {
            "repo": self.repo,
            "asset": {k: self.asset.get(k) for k in keep},
            "version": self.version,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Release":
        return cls(data["repo"], data["asset"], data.get("version"))


def fetch_release(repo: str, suffix: str) -> Release:
    try:
        release = http_get_json(f"https://api.github.com/repos/{repo}/releases/latest")
    except Exception as e:
        raise UpdateError(f"Could not reach GitHub for {repo}: {e}") from e

    asset = find_asset(release, suffix)
    if not asset:
        raise UpdateError(f"No release asset ending in {suffix} in {repo}")
    version = parse_version(
        release.get("tag_name"), release.get("name"), asset.get("name")
    )
    return Release(repo, asset, version)


def freshness(major: int | None, stable: int | None) -> str:
    if major is None or stable is None:
        return "unknown"
    gap = stable - major
    if gap <= 0:
        return "current"
    if gap < OUTDATED_GAP:
        return "behind"
    return "outdated"


def load_state() -> dict:
    try:
        state = json.loads(read_text_file(STATE_FILE) or "{}")
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def drop_keys(state: dict, *keys: str) -> bool:
    """Remove keys from state; whether any of them was there."""
    return bool([key for key in keys if state.pop(key, None) is not None])


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def is_fresh(entry: dict | None) -> bool:
    if not isinstance(entry, dict):
        return False
    age = time.time() - entry.get("fetched_at", 0)
    return 0 <= age < CACHE_SECONDS


def chrome_stable_major(state: dict) -> int | None:
    """Chrome stable's major version, asked for at most once an hour."""
    cached = state.get("stable")
    if is_fresh(cached):
        return cached["major"]
    try:
        data = http_get_json(CHROME_STABLE_URL)
        version = data["channels"]["Stable"]["version"]
        major = major_of(parse_version(version))
        if major is None:
            raise ValueError(f"unexpected version {version!r}")
    except Exception as e:
        log(f"Could not get the Chrome stable version: {e}")
        return cached.get("major") if isinstance(cached, dict) else None
    state["stable"] = {"major": major, "version": version, "fetched_at": time.time()}
    save_state(state)
    return major


def effective_stable(state: dict, majors: list[int | None]) -> int | None:
    """Chrome stable, or a higher version a build has already been released as.

    The Chrome stable lookup can lag behind, the GitHub releases can't.
    """
    reference = chrome_stable_major(state)
    known = [m for m in majors if m is not None]
    if reference is not None:
        # A build can be a version ahead of a lagging lookup, not more: a
        # larger number is a misread version (e.g. a date in a tag).
        known = [m for m in known if m <= reference + 1] + [reference]
    return max(known) if known else None


def scan_builds(state: dict) -> list[Release]:
    """Latest release of every known build, asked for at most once an hour."""
    cached = state.get("scan")
    if is_fresh(cached):
        try:
            return [Release.from_json(r) for r in cached["releases"]]
        except (KeyError, TypeError):
            pass

    log("Checking the other builds")
    releases = []
    for build in KNOWN_BUILDS:
        try:
            releases.append(fetch_release(build["repo"], build["asset_suffix"]))
        except UpdateError as e:
            log(str(e))
    if releases:
        state["scan"] = {
            "releases": [r.to_json() for r in releases],
            "fetched_at": time.time(),
        }
        save_state(state)
    return releases


# ---------------------------------------------------------------- installed build


def read_installed() -> dict | None:
    """What the version file says is installed.

    Older versions of this script wrote only the asset name, for the
    configured repo.
    """
    text = read_text_file(VERSION_FILE)
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("asset") and data.get("repo"):
            return data
    except ValueError:
        pass
    return {"repo": REPO, "asset": text, "version": parse_version(text)}


def write_installed(release: Release) -> None:
    data = {
        "repo": release.repo,
        "asset": release.name,
        "version": release.version,
        # The configured build at the time: if it differs from repo, this
        # release is a fallback standing in for it.
        "tracked": REPO,
    }
    VERSION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def local_archive_name(release: Release) -> str:
    """The file name on disk. Hibbiki calls every release chrome.7z, so a
    name without the version gets it put in front."""
    if release.version and release.version not in release.name:
        return f"{release.version}_{release.name}"
    return release.name


def is_installed(release: Release, installed: dict | None) -> bool:
    if not installed or not (EXTRACT_DIR / "chrome.exe").exists():
        return False
    if installed.get("asset") != release.name:
        return False
    if not same_repo(installed.get("repo"), release.repo):
        return False
    # Same file name, but maybe a newer release (e.g. chrome.7z).
    old, new = installed.get("version"), release.version
    if new is None:
        return True
    if old is None:
        # An old-style version file: only the name, which may not tell.
        return new in release.name
    return old == new


def install(release: Release, installed: dict | None) -> None:
    """Install release unless it is already in place."""
    asset_name = local_archive_name(release)
    archive = DOWNLOAD_DIR / asset_name
    chrome_exe = EXTRACT_DIR / "chrome.exe"
    digest = release.asset.get("digest")

    if is_installed(release, installed):
        log(f"Already up to date: {asset_name}")
        if not same_repo(installed.get("tracked"), REPO):
            # A kept fallback became the tracked build: no longer a stand-in.
            VERSION_FILE.write_text(json.dumps({**installed, "tracked": REPO}, indent=2), encoding="utf-8")
        return

    if archive.exists() and not file_matches_digest(archive, digest):
        log(f"{asset_name} on disk doesn't match its checksum, downloading it again")
        archive.unlink()

    if archive.exists():
        log(f"Already downloaded: {asset_name}")
    else:
        log(f"Downloading {asset_name} from {release.repo}")
        try:
            download_file(release.asset["browser_download_url"], archive)
        except Exception as e:
            raise UpdateError(f"Download of {asset_name} failed: {e}") from e
        if not file_matches_digest(archive, digest):
            archive.unlink(missing_ok=True)
            raise UpdateError(f"Download of {asset_name} doesn't match its checksum")

    remove_old_archives(asset_name)

    # Checked again: Chromium may have been started during the download. The
    # version file stays untouched, so the update is retried next time.
    if chrome_exe.exists() and is_chromium_running(chrome_exe):
        log("Chromium was started meanwhile, the update waits for the next start")
        return

    try:
        extract_flat(archive, EXTRACT_DIR)
    except Exception as e:
        # A damaged archive must not be kept, or every later run repeats it.
        archive.unlink(missing_ok=True)
        raise UpdateError(f"Could not extract {asset_name}: {e}") from e

    # Written only here: the version file records what is installed, not what
    # was downloaded.
    write_installed(release)
    log(f"Installed {asset_name}")


# ---------------------------------------------------------------- choosing a build


def describe_build(build: dict | None) -> str:
    if not build:
        return "Features unknown"
    parts = [GOOGLE_LABELS.get(build["google"], build["google"])]
    for key, label in FEATURES:
        parts.append(label if build[key] else f"no {label}")
    text = " · ".join(parts)
    return text[0].upper() + text[1:]


def compare_builds(build: dict | None, base: dict | None) -> list[str]:
    """What build gives up compared with base."""
    if not build or not base:
        return []
    warnings = []
    if PRIVACY_RANK.get(build["google"], 2) > PRIVACY_RANK.get(base["google"], 2):
        warnings.append(
            f"Less private than {base['name']}: "
            f"{GOOGLE_LABELS.get(build['google'], build['google'])}"
        )
    lost = [label for key, label in FEATURES if base[key] and not build[key]]
    if lost:
        warnings.append(f"Compared with {base['name']}, no {', no '.join(lost)}")
    return warnings


def status_text(major: int | None, stable: int | None) -> str:
    state = freshness(major, stable)
    if state == "unknown":
        return "version unknown"
    if state == "current":
        return "up to date"
    gap = stable - major
    return f"{gap} version{'s' if gap != 1 else ''} behind"


def status_suffix(mode: str, release: Release) -> str:
    if mode in ("change", "switchback") and same_repo(release.repo, REPO):
        return ", your build"
    if mode == "switchback":
        return ", installed"
    return ""


def picker_spec(
    mode: str,
    releases: list[Release],
    stable: int | None,
    original: Release | None = None,
    installed_major: int | None = None,
) -> dict:
    """Everything the picker shows, independent of how it is shown.

    mode "first": nothing is installed yet, the choice becomes the build to
    track. mode "change": started with --choose-build, the choice replaces
    the tracked build. mode "fallback": the tracked build is outdated, the
    choice is used until it catches up. mode "switchback": releases are the
    caught-up tracked build and the installed fallback.
    """
    base = known_build(REPO)

    def order(release: Release) -> tuple[int, int]:
        build = known_build(release.repo)
        if not build:
            return (len(PRIVACY_RANK), len(KNOWN_BUILDS))
        return (PRIVACY_RANK.get(build["google"], 2), KNOWN_BUILDS.index(build))

    options = []
    for release in sorted(releases, key=order):
        build = known_build(release.repo)
        notes = []
        if build and build.get("note"):
            notes.append(build["note"])
        if build and not build["widevine"] and build.get("widevine_by_hand"):
            notes.append("Widevine can be added by hand, see the README")
        warnings = compare_builds(build, base) if mode != "first" else []
        if installed_major and release.major and release.major < installed_major:
            warnings.append(
                f"Older than the installed Chromium {installed_major}: "
                "your profile may not work with it"
            )
        options.append(
            {
                "release": release,
                "title": f"{display_name(release.repo)} {release.version or ''}".strip(),
                "status": status_text(release.major, stable)
                + status_suffix(mode, release),
                "freshness": freshness(release.major, stable),
                "enabled": freshness(release.major, stable) != "outdated",
                "features": describe_build(build),
                "warnings": warnings,
                "notes": notes,
            }
        )

    preselect = None
    for i, option in enumerate(options):
        if option["enabled"] and (mode == "fallback" or same_repo(option["release"].repo, REPO)):
            preselect = i
            break
    if preselect is None:
        preselect = next((i for i, o in enumerate(options) if o["enabled"]), None)

    name = display_name(REPO)
    if mode == "first":
        headline = "Choose a Chromium build"
        intro = (
            "Chromium-Py-Updater downloads the build you choose and keeps it up to "
            "date. To switch builds later, double click choose-build.bat."
        )
        ok_label = "Install"
        cancel_label = "Quit without installing"
    elif mode == "change":
        headline = "Choose a Chromium build"
        intro = (
            f"Your build is {name}. Chromium-Py-Updater installs the build you "
            "choose instead and keeps it up to date. Your profile stays as it is."
        )
        ok_label = "Install"
        cancel_label = "Quit without changes"
    elif mode == "fallback":
        gap = stable - original.major
        headline = f"{name} is behind Chrome stable"
        intro = (
            f"{name} {original.version} is {gap} major versions behind Chrome "
            f"{stable}, so it is missing security fixes. Choose a build to use until "
            f"{name} catches up. Chromium-Py-Updater keeps checking {name} and "
            "offers to switch back once it has caught up."
        )
        ok_label = "Use this build"
        cancel_label = f"Keep {name}"
    else:  # switchback
        kept = next(r for r in releases if not same_repo(r.repo, REPO))
        kept_name = display_name(kept.repo)
        headline = f"{name} has caught up"
        if freshness(original.major, stable) == "current":
            caught_up = "is up to date again"
        else:
            caught_up = f"is only {status_text(original.major, stable)} Chrome {stable} again"
        intro = (
            f"{name} {original.version} {caught_up}, so the stand-in "
            f"{kept_name} is no longer needed. Switch back to {name}, or keep "
            f"{kept_name} as your build from now on. Your profile stays as it is."
        )
        ok_label = "Use this build"
        cancel_label = f"Keep {kept_name}"

    return {
        "headline": headline,
        "intro": intro,
        "options": options,
        "preselect": preselect,
        "ok_label": ok_label,
        "cancel_label": cancel_label,
    }


def pick_in_console(spec: dict) -> int | None:
    console(spec["headline"])
    console(spec["intro"])
    console()
    for i, option in enumerate(spec["options"], 1):
        marker = "" if option["enabled"] else "  [too far behind]"
        console(f"  {i}  {option['title']} ({option['status']}){marker}")
        for line in [option["features"], *option["warnings"], *option["notes"]]:
            console(f"     {line}")
    default = spec["preselect"]
    hint = f"Enter = {default + 1}, " if default is not None else ""
    while True:
        try:
            answer = input(f"\nNumber ({hint}n = {spec['cancel_label']}): ").strip()
        except EOFError:
            return None
        if answer.lower() == "n":
            return None
        if not answer and default is not None:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(spec["options"]):
            index = int(answer) - 1
            if spec["options"][index]["enabled"]:
                return index
        console("Please enter one of the numbers shown.")


def ensure_console() -> bool:
    """Make sure there is a console to show the picker in.

    Started with pythonw.exe (from a link, say) there is none, so one is
    opened. It stays for the download progress and closes when the launcher
    exits. Returns False when output is redirected or no console can be had.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not console_is_interactive():
        if sys.stdout is not None or not kernel32.AllocConsole():
            return False
        try:
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        except OSError:
            return False
    # Otherwise the title is the path of python.exe.
    kernel32.SetConsoleTitleW(APP_NAME)
    return True


PYTHON_EXES = {"py.exe", "pyw.exe", "python.exe", "pythonw.exe"}


def process_name(pid: int) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint32(len(buf))
        if not kernel32.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0, buf, ctypes.byref(size)):
            return ""
        return Path(buf.value).name.lower()
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def console_is_ours() -> bool:
    """True when no shell shares the console: it was opened for us (double
    click, or ensure_console()), so resizing it bothers nobody.

    choose-build.bat runs in a console of its own and says so.
    """
    if os.environ.get("CPL_OWN_CONSOLE") == "1":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pids = (ctypes.c_uint32 * 16)()
    count = kernel32.GetConsoleProcessList(pids, len(pids))
    if not 0 < count <= len(pids):
        return False
    return all(pid == os.getpid() or process_name(pid) in PYTHON_EXES for pid in pids[:count])


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [(name, ctypes.c_short) for name in ("Left", "Top", "Right", "Bottom")]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", COORD),
        ("dwCursorPosition", COORD),
        ("wAttributes", ctypes.c_ushort),
        ("srWindow", SMALL_RECT),
        ("dwMaximumWindowSize", COORD),
    ]


def grow_console(cols: int, rows: int) -> None:
    """Make the console window at least cols x rows, as far as the screen
    allows, and keep it on the screen.

    Only works in the classic console window; Windows Terminal ignores it.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    kernel32.GetLargestConsoleWindowSize.restype = COORD
    kernel32.GetConsoleWindow.restype = ctypes.c_void_p
    handle = ctypes.c_void_p(kernel32.GetStdHandle(-11))
    info = CONSOLE_SCREEN_BUFFER_INFO()
    if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
        return
    window = info.srWindow
    largest = kernel32.GetLargestConsoleWindowSize(handle)
    cur_cols, cur_rows = window.Right - window.Left + 1, window.Bottom - window.Top + 1
    new_cols = min(max(cur_cols, cols), largest.X or cols)
    new_rows = min(max(cur_rows, rows), largest.Y or rows)
    if (new_cols, new_rows) == (cur_cols, cur_rows):
        return
    # The buffer has to be at least as large as the window.
    buffer = COORD(max(info.dwSize.X, new_cols), max(info.dwSize.Y, window.Top + new_rows))
    kernel32.SetConsoleScreenBufferSize(handle, buffer)
    rect = SMALL_RECT(0, window.Top, new_cols - 1, window.Top + new_rows - 1)
    kernel32.SetConsoleWindowInfo(handle, True, ctypes.byref(rect))

    # A taller window may now reach below the screen: move it back up.
    hwnd = ctypes.c_void_p(kernel32.GetConsoleWindow())
    if not hwnd:
        return

    class RECT(ctypes.Structure):
        _fields_ = [(name, ctypes.c_long) for name in ("left", "top", "right", "bottom")]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint32), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_uint32)]

    user32.MonitorFromWindow.restype = ctypes.c_void_p
    box = RECT()
    monitor = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    MONITOR_DEFAULTTONEAREST = 2
    if not user32.GetWindowRect(hwnd, ctypes.byref(box)) or not user32.GetMonitorInfoW(
        ctypes.c_void_p(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)), ctypes.byref(monitor)
    ):
        return
    work = monitor.rcWork
    left = max(work.left, min(box.left, work.right - (box.right - box.left)))
    top = max(work.top, min(box.top, work.bottom - (box.bottom - box.top)))
    if (left, top) != (box.left, box.top):
        SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10
        user32.SetWindowPos(hwnd, None, left, top, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)


def enable_ansi() -> bool:
    """Switch the console to escape sequences (colours, cursor movement)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    handle = ctypes.c_void_p(kernel32.GetStdHandle(-11))
    mode = ctypes.c_uint32()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    return bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))


def read_key() -> str | None:
    """The next key as a name, or None when nothing was pressed for a moment."""
    import msvcrt

    if not msvcrt.kbhit():
        time.sleep(0.05)
        return None
    key = msvcrt.getwch()
    if key in ("\x00", "\xe0"):
        return {"H": "up", "P": "down", "G": "home", "O": "end"}.get(msvcrt.getwch(), "")
    return {"\r": "enter", "\x1b": "cancel", "\x03": "cancel"}.get(key, key)


# Escape sequences for the console picker.
RESET, BOLD, REVERSE = "\x1b[0m", "\x1b[1m", "\x1b[7m"
ACCENT, GREEN, YELLOW, RED, GREY = "\x1b[96m", "\x1b[92m", "\x1b[93m", "\x1b[91m", "\x1b[90m"
STATUS_COLORS = {"current": GREEN, "behind": YELLOW}
ESCAPE_SEQUENCE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def visible_len(text: str) -> int:
    return len(ESCAPE_SEQUENCE.sub("", text))


def crop(text: str, width: int) -> str:
    """Cut text to width visible characters, keeping its escape sequences."""
    if visible_len(text) <= width:
        return text
    out, shown, pos = [], 0, 0
    while pos < len(text) and shown < width:
        match = ESCAPE_SEQUENCE.match(text, pos)
        if match:
            out.append(match.group())
            pos = match.end()
        else:
            out.append(text[pos])
            shown += 1
            pos += 1
    return "".join(out) + RESET


# Widest picker: 100 columns of text plus the margins.
PICKER_COLS = 103


def render_picker(spec: dict, selected: int, cols: int, rows: int) -> list[str]:
    """The picker's lines for a console of the given size.

    When everything doesn't fit, the blank lines between builds go first,
    then the details of all but the selected build.
    """
    import textwrap

    # One column of margin on the left, and the last column stays free:
    # writing into it would wrap the line.
    width = max(20, min(cols - 2, 100))

    def wrap(text: str, indent: int) -> list[str]:
        return textwrap.wrap(text, width - indent, break_on_hyphens=False) or [""]

    def title_line(head: str, status: str) -> list[str]:
        """Title and status on one line, or the status below when too narrow."""
        if visible_len(head + status) + 2 <= width:
            return [f"{head}  {status}"]
        return [head, f"     {status}"]

    def build(compact: bool, spaced: bool) -> list[str]:
        lines = [f" {ACCENT}{BOLD}{APP_NAME}{RESET}", ""]
        lines += [f" {BOLD}{line}{RESET}" for line in wrap(spec["headline"], 1)]
        lines += [f" {line}" for line in wrap(spec["intro"], 1)]
        lines.append("")
        for i, option in enumerate(spec["options"]):
            if i and spaced:
                lines.append("")
            status = option["status"]
            if not option["enabled"]:
                lines += title_line(
                    f"   {GREY}○ {option['title']}", f"({status}, too far behind){RESET}"
                )
                if not compact:
                    lines += [f"     {GREY}{line}{RESET}" for line in wrap(option["features"], 5)]
                continue
            color = STATUS_COLORS.get(option["freshness"], GREY)
            if i == selected:
                head = f" {ACCENT}{BOLD}► ● {option['title']}{RESET}"
            else:
                head = f"   ○ {option['title']}"
            lines += title_line(head, f"{color}({status}){RESET}")
            if compact and i != selected:
                continue
            lines += [f"     {line}" for line in wrap(option["features"], 5)]
            for warning in option["warnings"]:
                lines += [f"     {RED}{line}{RESET}" for line in wrap(warning, 5)]
            for note in option["notes"]:
                lines += [f"     {GREY}{line}{RESET}" for line in wrap(note, 5)]
        lines.append("")
        keys = [("↑↓", "Choose"), ("Enter", spec["ok_label"]), ("Esc", spec["cancel_label"])]
        lines.append(" " + "   ".join(f"{REVERSE} {k} {RESET} {label}" for k, label in keys))
        return lines

    for compact, spaced in ((False, True), (False, False), (True, False)):
        lines = build(compact, spaced)
        # +1 for the top margin, and the last row stays free.
        if len(lines) + 1 < rows:
            break
    # A line wider than the window would wrap and push the picker down.
    return [""] + [" " + crop(line, width) if line else "" for line in lines]


def console_size() -> tuple[int, int]:
    """Columns and rows of the visible console window."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    info = CONSOLE_SCREEN_BUFFER_INFO()
    # By the process's handle, so it also works for a console opened by
    # ensure_console(), where Python's own stdout handle doesn't exist.
    if kernel32.GetConsoleScreenBufferInfo(ctypes.c_void_p(kernel32.GetStdHandle(-11)), ctypes.byref(info)):
        window = info.srWindow
        return window.Right - window.Left + 1, window.Bottom - window.Top + 1
    return tuple(shutil.get_terminal_size())


def pick_with_keys(spec: dict, keys=read_key, out=None, size=console_size) -> int | None:
    """The console picker: arrow keys choose, Enter confirms, Esc cancels."""
    out = out or sys.stdout
    enabled = [i for i, option in enumerate(spec["options"]) if option["enabled"]]
    selected = spec["preselect"] if spec["preselect"] is not None else enabled[0]
    try:
        # Started from a link, the console may open behind other windows.
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass

    # Alternate screen, cursor hidden: the picker gets the whole window, and
    # the log lines from before are back afterwards.
    out.write("\x1b[?1049h\x1b[?25l")
    drawn = None
    try:
        while True:
            cols, rows = size()
            if drawn != (selected, cols, rows):
                lines = render_picker(spec, selected, cols, rows)
                out.write("\x1b[H" + "\n".join(line + "\x1b[K" for line in lines) + "\x1b[J")
                out.flush()
                drawn = (selected, cols, rows)
            key = keys()
            if key == "enter":
                return selected
            if key == "cancel":
                return None
            pos = enabled.index(selected)
            if key == "up":
                selected = enabled[max(pos - 1, 0)]
            elif key == "down":
                selected = enabled[min(pos + 1, len(enabled) - 1)]
            elif key == "home":
                selected = enabled[0]
            elif key == "end":
                selected = enabled[-1]
            elif key and key.isdigit() and int(key) - 1 in enabled:
                selected = int(key) - 1
    finally:
        out.write("\x1b[?25h\x1b[?1049l")
        out.flush()


def show_picker(spec: dict) -> int | None:
    """Show the picker in the console, opening one if needed.

    Returns the chosen option's index, or None when the user declined.
    Raises when there is no way to show it at all.
    """
    if not ensure_console():
        raise RuntimeError("no console to show it in")
    if enable_ansi():
        try:
            if console_is_ours():
                # Room for the whole picker, with blank lines between builds.
                full = render_picker(spec, spec["preselect"] or 0, PICKER_COLS, 1000)
                grow_console(PICKER_COLS, len(full) + 2)
        except Exception as e:
            log(f"Could not resize the console: {e}")
        return pick_with_keys(spec)
    # A console without escape sequences (Windows before 10): numbers.
    return pick_in_console(spec)


def pick_build(spec: dict) -> tuple[bool, Release | None]:
    """Show the picker. Returns (shown, choice); choice is None when the
    user declined, or when there was nothing to show or no way to show it."""
    if not any(option["enabled"] for option in spec["options"]):
        log("No other build is recent enough to offer")
        return False, None
    try:
        index = show_picker(spec)
    except Exception as e:
        log(f"Could not show the build picker: {e}")
        return False, None
    if index is not None:
        log(f"Chosen: {spec['options'][index]['title']}")
    return True, None if index is None else spec["options"][index]["release"]


def set_tracked_build(release: Release) -> None:
    """Make release's repo the configured one, in memory and in config.jsonc."""
    global REPO, ASSET_SUFFIX
    build = known_build(release.repo)
    REPO = release.repo
    ASSET_SUFFIX = build["asset_suffix"] if build else ASSET_SUFFIX

    try:
        text = CONFIG_FILE.read_text(encoding="utf-8")
    except OSError:
        return
    missing = []
    for key, value in (("repo", REPO), ("asset_suffix", ASSET_SUFFIX)):
        pattern = re.compile(rf'^(\s*"{key}"\s*:\s*)"(?:[^"\\]|\\.)*"', re.MULTILINE)
        new = json.dumps(value)
        if pattern.search(text):
            text = pattern.sub(lambda m: m.group(1) + new, text, count=1)
        else:
            missing.append(f'  "{key}": {new}')
    if missing:
        # Added at the top, before the comments of the generated file.
        start = text.index("{") + 1
        rest = "\n".join(
            line for line in text[start:].splitlines() if not line.lstrip().startswith("//")
        )
        comma = "" if rest.strip().startswith("}") else ","
        block = "\n  // Your Chromium build, chosen in the picker (choose-build.bat)\n"
        block += ",\n".join(missing) + comma + "\n"
        # A blank line before the settings that follow.
        text = text[:start] + block + ("\n" if comma else "") + text[start:].lstrip("\n")
    try:
        CONFIG_FILE.write_text(text, encoding="utf-8")
        log(f"Saved {display_name(REPO)} as your build in {CONFIG_FILE.name}")
    except OSError as e:
        log(f"Could not save your choice in {CONFIG_FILE.name}: {e}")


class PickerClosed(Exception):
    """The first-run picker was closed without choosing: install nothing."""


def choose_build(state: dict, mode: str = "first") -> bool:
    """Let the user choose the build to track: on the first start (nothing
    installed yet) or with --choose-build (mode "change").

    Returns whether the picker was shown. Raises PickerClosed when the user
    closed it without choosing.
    """
    releases = scan_builds(state)
    if not known_build(REPO):
        # A repo of the user's own from config.jsonc is offered as well.
        try:
            releases.append(fetch_release(REPO, ASSET_SUFFIX))
        except UpdateError as e:
            log(str(e))
    if not releases:
        if mode == "change":
            raise UpdateError("Could not reach GitHub for the list of builds")
        return False
    stable = effective_stable(state, [r.major for r in releases])
    installed = read_installed() if mode == "change" else None
    installed_major = major_of(installed.get("version")) if installed else None
    shown, choice = pick_build(
        picker_spec(mode, releases, stable, installed_major=installed_major)
    )
    if shown and choice is None:
        raise PickerClosed()
    if choice is None:
        # The picker couldn't be shown: go on with the configured build.
        log(f"No build chosen, using {display_name(REPO)} from {CONFIG_FILE.name}")
    elif not same_repo(choice.repo, REPO):
        set_tracked_build(choice)
        # A "no" to a fallback was about the old build.
        if state.pop("declined_for_stable", None) is not None:
            save_state(state)
    return shown


def change_build() -> None:
    """--choose-build: choose another build and install it."""
    chrome_exe = EXTRACT_DIR / "chrome.exe"
    if chrome_exe.exists() and is_chromium_running(chrome_exe):
        raise RuntimeError("Chromium is running. Close it, then try again.")
    state = load_state()
    if not choose_build(state, "change"):
        raise RuntimeError("Could not show the build picker")
    # The chosen build is installed as it is, also while a fallback was in
    # use: choosing is the answer to every open question about fallbacks.
    if drop_keys(state, "fallback", "declined_for_stable"):
        save_state(state)
    update_to_latest(allow_picker=False, offer_fallback=False)


def choose_release(
    original: Release, installed: dict | None, state: dict, allow_picker: bool = True
) -> Release:
    """The release to install: the tracked build's, or a fallback's.

    A fallback is only used while the tracked build is two or more major
    versions behind Chrome stable.
    """
    name = display_name(REPO)
    # Only a release installed as a stand-in for the configured build counts;
    # after a change in config.jsonc the new build is simply installed.
    on_fallback = (
        installed is not None
        and not same_repo(installed.get("repo"), REPO)
        and same_repo(installed.get("tracked"), REPO)
        and known_build(installed.get("repo")) is not None
    )
    # A fallback chosen earlier whose install didn't finish (download failed,
    # or Chromium was started meanwhile) is picked up again, not asked anew.
    chosen = state.get("fallback")
    if not (isinstance(chosen, dict) and same_repo(chosen.get("for"), REPO)):
        chosen = None
    fallback_build = known_build(installed["repo"] if on_fallback else (chosen or {}).get("repo"))

    current_fallback = None
    if fallback_build:
        try:
            current_fallback = fetch_release(fallback_build["repo"], fallback_build["asset_suffix"])
        except UpdateError as e:
            log(str(e))

    majors = [original.major] + ([current_fallback.major] if current_fallback else [])
    stable = effective_stable(state, majors)

    if freshness(original.major, stable) != "outdated":
        if on_fallback:
            return offer_switch_back(
                original, installed, current_fallback, stable, state, allow_picker
            )
        if drop_keys(state, "fallback"):
            save_state(state)
        return original

    gap = stable - original.major
    log(f"{name} {original.version} is {gap} major versions behind Chrome stable {stable}")

    if fallback_build and not current_fallback:
        # GitHub didn't answer for the fallback: stay with what is installed.
        if on_fallback and (EXTRACT_DIR / "chrome.exe").exists():
            return Release(installed["repo"], {"name": installed["asset"]}, installed.get("version"))
        if on_fallback:
            raise UpdateError(f"Could not check {fallback_build['name']}")
        return original

    if current_fallback and freshness(current_fallback.major, stable) != "outdated":
        log(f"Using {fallback_build['name']} until {name} catches up")
        return current_fallback

    # Remembered by the version known before the scan, which is what the
    # next start sees at this point too.
    if state.get("declined_for_stable") == stable:
        log(f"No fallback wanted for Chrome {stable}, keeping {name}")
        return original
    if not allow_picker:
        return original
    declined_key = stable

    candidates = [r for r in scan_builds(state) if not same_repo(r.repo, REPO)]
    stable = effective_stable(state, [original.major, *[r.major for r in candidates]])
    shown, choice = pick_build(picker_spec("fallback", candidates, stable, original))
    if choice is None:
        # The button says "Keep <configured build>", so that's what happens,
        # also when an outdated fallback was in use.
        log(f"No fallback build chosen, using {name}")
        state.pop("fallback", None)
        if shown:
            # Only a real "no" is remembered; a picker that couldn't be shown
            # or had nothing to offer is tried again on the next start.
            state["declined_for_stable"] = declined_key
        save_state(state)
        return original

    state.pop("declined_for_stable", None)
    state["fallback"] = {"repo": choice.repo, "for": REPO}
    save_state(state)
    log(f"Using {display_name(choice.repo)} until {name} catches up")
    return choice


def offer_switch_back(
    original: Release,
    installed: dict,
    current_fallback: Release | None,
    stable: int | None,
    state: dict,
    allow_picker: bool,
) -> Release:
    """The tracked build has caught up while a fallback is installed: ask
    whether to switch back to it. Never switches by itself."""
    name = display_name(REPO)
    kept = current_fallback or Release(
        installed["repo"], {"name": installed["asset"]}, installed.get("version")
    )
    kept_name = display_name(kept.repo)
    if not current_fallback and not (EXTRACT_DIR / "chrome.exe").exists():
        # Nothing left to keep: the fallback is neither reachable nor on disk.
        log(f"{kept_name} can't be checked and isn't installed, using {name}")
        return original

    installed_major = major_of(installed.get("version"))
    if original.major is None or installed_major is None:
        log(f"Can't compare {name} with the installed {kept_name}, keeping {kept_name}")
        return kept
    if original.major < installed_major:
        # Going back to an older Chromium can break the profile.
        log(
            f"{name} {original.version} has caught up, but is older than the "
            f"installed {kept_name} {installed.get('version')}: keeping {kept_name}"
        )
        return kept
    if not allow_picker:
        return kept

    shown, choice = pick_build(picker_spec("switchback", [original, kept], stable, original))
    if not shown:
        # Asked again on the next start.
        return kept
    drop_keys(state, "fallback", "declined_for_stable")
    save_state(state)
    if choice is not None and same_repo(choice.repo, REPO):
        log(f"Switching back to {name} {original.version}")
        return original
    # Kept, it is no fallback anymore but your build, as if chosen on the
    # first start.
    log(f"Keeping {kept_name} as your build")
    set_tracked_build(kept)
    return kept


def update_to_latest(allow_picker: bool = True, offer_fallback: bool = True) -> None:
    installed = read_installed()
    log("Checking latest release")
    original = fetch_release(REPO, ASSET_SUFFIX)
    target = original
    if OFFER_FALLBACK and offer_fallback:
        target = choose_release(original, installed, load_state(), allow_picker)
    install(target, installed)


APP_ID = "ChromiumLauncher"
APP_NAME = "Chromium-Py-Updater"
PROG_ID = "ChromiumLauncherHTML"
APP_KEY = rf"Software\Clients\StartMenuInternet\{APP_ID}"
PROG_KEY = rf"Software\Classes\{PROG_ID}"
REGISTERED_APPS_KEY = r"Software\RegisteredApplications"
URL_SCHEMES = ("http", "https")
FILE_TYPES = (".htm", ".html", ".xhtml", ".pdf")

SHCNE_ASSOCCHANGED = 0x08000000


def set_reg_value(path: str, name: str, value: str) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def delete_reg_tree(path: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            subkeys = []
            while True:
                try:
                    subkeys.append(winreg.EnumKey(key, len(subkeys)))
                except OSError:
                    break
    except FileNotFoundError:
        return
    for subkey in subkeys:
        delete_reg_tree(rf"{path}\{subkey}")
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)


def register() -> None:
    """Offer the launcher as a browser in Windows' Default apps settings.

    Windows lets only the user pick the default browser, so this registers it
    for the current user and then opens the settings page to choose it.
    """
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    # "--" ends Chromium's switch parsing, so a crafted link cannot smuggle
    # in a command line switch.
    command = f'"{pythonw}" "{Path(__file__).resolve()}" -- "%1"'
    icon = f"{EXTRACT_DIR / 'chrome.exe'},0"
    capabilities = rf"{APP_KEY}\Capabilities"

    set_reg_value(PROG_KEY, "", "Chromium HTML Document")
    set_reg_value(rf"{PROG_KEY}\DefaultIcon", "", icon)
    set_reg_value(rf"{PROG_KEY}\shell\open\command", "", command)
    # Without this, Windows names the handler after pythonw.exe: "Python".
    set_reg_value(rf"{PROG_KEY}\Application", "ApplicationName", APP_NAME)
    set_reg_value(rf"{PROG_KEY}\Application", "ApplicationIcon", icon)

    set_reg_value(APP_KEY, "", APP_NAME)
    set_reg_value(capabilities, "ApplicationName", APP_NAME)
    set_reg_value(capabilities, "ApplicationDescription", "Keeps a portable Chromium up to date")
    set_reg_value(capabilities, "ApplicationIcon", icon)
    for scheme in URL_SCHEMES:
        set_reg_value(rf"{capabilities}\URLAssociations", scheme, PROG_ID)
    for file_type in FILE_TYPES:
        set_reg_value(rf"{capabilities}\FileAssociations", file_type, PROG_ID)
    set_reg_value(REGISTERED_APPS_KEY, APP_ID, capabilities)

    ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, 0, None, None)
    log(f"Registered as a browser: {command}")
    os.startfile(f"ms-settings:defaultapps?registeredAppUser={APP_ID}")


def unregister() -> None:
    delete_reg_tree(APP_KEY)
    delete_reg_tree(PROG_KEY)
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REGISTERED_APPS_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, APP_ID)
    except FileNotFoundError:
        pass

    ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, 0, None, None)
    log("Removed the browser registration")


def opens_link(args: list[str]) -> bool:
    """True when started with a URL or file, as opposed to switches only."""
    return any(not arg.startswith("-") for arg in args)


def main() -> None:
    args = sys.argv[1:]
    if args in (["--register"], ["--unregister"]):
        try:
            register() if args == ["--register"] else unregister()
        except Exception as e:
            show_error(str(e), "Failed to change the browser registration")
            sys.exit(1)
        return

    ensure_dir(DOWNLOAD_DIR)
    ensure_dir(PROFILE_DIR)

    chrome_exe = EXTRACT_DIR / "chrome.exe"

    if args == ["--choose-build"]:
        try:
            change_build()
        except PickerClosed:
            log("Build picker closed, nothing changed")
            return
        except Exception as e:
            show_error(str(e), "Failed to change the Chromium build")
            sys.exit(1)
        launch_chromium(chrome_exe, [])
        return

    # A link should open right away; the next plain start does the update.
    if chrome_exe.exists() and opens_link(args):
        log("Opening a link, skipping the update check")
    elif chrome_exe.exists() and is_chromium_running(chrome_exe):
        log("Chromium is running, skipping the update check")
    else:
        try:
            picker_shown = False
            if not chrome_exe.exists() and OFFER_FALLBACK:
                picker_shown = choose_build(load_state())
            # One picker per start is enough.
            update_to_latest(allow_picker=not picker_shown)
        except PickerClosed:
            log("Build picker closed, quitting without installing")
            return
        except Exception as e:
            # An update problem must never stand between the user and the
            # browser, whatever kind of problem it is.
            if not chrome_exe.exists():
                raise RuntimeError(f"{e}, and no Chromium is installed") from e
            log(f"{e} - launching the installed version")

    if not chrome_exe.exists():
        raise RuntimeError(f"chrome.exe not found in {EXTRACT_DIR}")

    launch_chromium(chrome_exe, args)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        show_error(str(e))
        sys.exit(1)
