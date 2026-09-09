import ctypes
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from string import Formatter


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.jsonc"

MB_OK = 0x00
MB_YESNO = 0x04
MB_ICONERROR = 0x10
MB_ICONQUESTION = 0x20
IDYES = 6


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{timestamp} {message}\n")
    except Exception:
        pass


def show_error(message: str) -> None:
    """Report a fatal error.

    The launcher is normally started from a shortcut, where stderr goes
    nowhere, so a message box is the only thing the user will actually see.
    """
    log(f"ERROR: {message}")
    try:
        ctypes.windll.user32.MessageBoxW(
            0,
            f"Failed to launch Chromium.\n\n{message}",
            "Chromium Launcher",
            MB_OK | MB_ICONERROR,
        )
    except Exception:
        pass
    print(f"Failed to launch Chromium: {message}", file=sys.stderr)

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
        "repo": "ungoogled-software/ungoogled-chromium-windows",
        "asset_suffix": "_windows_x64.zip",
        "download_dir": "download",
        "extract_dir": "bin",
        "profile_dir": "profile",
        "version_file": "latest.txt",
        "log_file": "launch.log",
        "temp_dir": "temp",
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
            write_default_config(CONFIG_FILE, default_config)
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
    API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
    ASSET_SUFFIX = CONFIG["asset_suffix"]

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
    with urllib.request.urlopen(req) as response:
        return json.load(response)


def download_file(url: str, dest: Path) -> None:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "chromium-launcher"},
    )
    with urllib.request.urlopen(req) as response, dest.open("wb") as f:
        shutil.copyfileobj(response, f)


def find_asset(release: dict) -> dict | None:
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.endswith(ASSET_SUFFIX):
            return asset
    return None


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


def remove_old_zips(keep_name: str) -> None:
    for zip_path in DOWNLOAD_DIR.glob("*.zip"):
        if zip_path.name != keep_name:
            zip_path.unlink(missing_ok=True)


def is_chromium_running(chrome_exe: Path) -> bool:
    chrome_exe_str = str(chrome_exe.resolve()).lower()

    ps_script = rf"""
$target = [System.IO.Path]::GetFullPath("{chrome_exe_str}")
$running = Get-CimInstance Win32_Process | Where-Object {{
    $_.Name -match '^(chrome|chromium)\.exe$' -and
    $_.ExecutablePath -and
    $_.ExecutablePath.ToLower() -eq $target
}}
if ($running) {{ exit 1 }} else {{ exit 0 }}
"""

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", ps_script],
        capture_output=True,
        text=True,
        check=False,
    )

    return result.returncode != 0


def ask_update_while_running() -> bool:
    message = (
        "Chromium is still running.\n\n"
        "Yes = skip the update and launch the current version\n"
        "No = cancel"
    )
    result = ctypes.windll.user32.MessageBoxW(
        0,
        message,
        "Chromium Update",
        MB_YESNO | MB_ICONQUESTION,
    )
    return result == IDYES


def extract_zip_flat(zip_file: Path, extract_dir: Path) -> None:
    with tempfile.TemporaryDirectory(dir=BASE_DIR) as tmp_dir:
        tmp_path = Path(tmp_dir)

        log("Extracting archive")
        with zipfile.ZipFile(zip_file, "r") as zf:
            zf.extractall(tmp_path)

        top_level_items = list(tmp_path.iterdir())

        if len(top_level_items) == 1 and top_level_items[0].is_dir():
            log("Flattening top-level folder")
            source_items = list(top_level_items[0].iterdir())
        else:
            log("Moving extracted contents")
            source_items = top_level_items

        recreate_dir(extract_dir)

        for item in source_items:
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


def fetch_latest_asset() -> dict:
    try:
        release = http_get_json(API_URL)
    except Exception as e:
        raise UpdateError(f"Could not reach GitHub: {e}") from e

    asset = find_asset(release)
    if not asset:
        raise UpdateError(f"No release asset ending in {ASSET_SUFFIX}")
    return asset


def update_to_latest() -> bool:
    """Install the latest release unless it is already in place.

    Returns False if the user chose to cancel instead of updating.
    """
    asset = fetch_latest_asset()
    asset_name = asset["name"]
    zip_file = DOWNLOAD_DIR / asset_name
    chrome_exe = EXTRACT_DIR / "chrome.exe"

    if read_text_file(VERSION_FILE) == asset_name and chrome_exe.exists():
        log(f"Already up to date: {asset_name}")
        return True

    if zip_file.exists():
        log(f"Already downloaded: {asset_name}")
    else:
        log(f"Downloading {asset_name}")
        try:
            download_file(asset["browser_download_url"], zip_file)
        except Exception as e:
            raise UpdateError(f"Download of {asset_name} failed: {e}") from e

    remove_old_zips(asset_name)

    if chrome_exe.exists() and is_chromium_running(chrome_exe):
        log("An update is pending while Chromium is running")
        if not ask_update_while_running():
            log("User cancelled because Chromium is still running")
            return False
        # The version file stays untouched, so the update is retried next time.
        log("User chose to skip the update and launch the current version")
        return True

    try:
        extract_zip_flat(zip_file, EXTRACT_DIR)
    except Exception as e:
        # A damaged archive must not be kept, or every later run repeats it.
        zip_file.unlink(missing_ok=True)
        raise UpdateError(f"Could not extract {asset_name}: {e}") from e

    # Written only here: the version file records what is installed, not what
    # was downloaded.
    VERSION_FILE.write_text(asset_name, encoding="utf-8")
    log(f"Installed {asset_name}")
    return True


def main() -> None:
    ensure_dir(DOWNLOAD_DIR)
    ensure_dir(PROFILE_DIR)

    chrome_exe = EXTRACT_DIR / "chrome.exe"

    try:
        if not update_to_latest():
            return
    except UpdateError as e:
        # An update problem must never stand between the user and the browser.
        if not chrome_exe.exists():
            raise RuntimeError(f"{e}, and no Chromium is installed") from e
        log(f"{e} - launching the installed version")

    if not chrome_exe.exists():
        raise RuntimeError(f"chrome.exe not found in {EXTRACT_DIR}")

    launch_chromium(chrome_exe, sys.argv[1:])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        show_error(str(e))
        sys.exit(1)
