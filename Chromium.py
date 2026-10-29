import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import winreg
import zipfile
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


def extract_flat(archive: Path, extract_dir: Path) -> None:
    with tempfile.TemporaryDirectory(dir=BASE_DIR) as tmp_dir:
        tmp_path = Path(tmp_dir)

        log("Extracting archive")
        extract_archive(archive, tmp_path)

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
    log("Checking latest release")
    try:
        release = http_get_json(API_URL)
    except Exception as e:
        raise UpdateError(f"Could not reach GitHub: {e}") from e

    asset = find_asset(release)
    if not asset:
        raise UpdateError(f"No release asset ending in {ASSET_SUFFIX}")
    return asset


def update_to_latest() -> None:
    """Install the latest release unless it is already in place."""
    asset = fetch_latest_asset()
    asset_name = asset["name"]
    archive = DOWNLOAD_DIR / asset_name
    chrome_exe = EXTRACT_DIR / "chrome.exe"

    if read_text_file(VERSION_FILE) == asset_name and chrome_exe.exists():
        log(f"Already up to date: {asset_name}")
        return

    if archive.exists():
        log(f"Already downloaded: {asset_name}")
    else:
        log(f"Downloading {asset_name}")
        try:
            download_file(asset["browser_download_url"], archive)
        except Exception as e:
            raise UpdateError(f"Download of {asset_name} failed: {e}") from e

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
    VERSION_FILE.write_text(asset_name, encoding="utf-8")
    log(f"Installed {asset_name}")


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

    # A link should open right away; the next plain start does the update.
    if chrome_exe.exists() and opens_link(args):
        log("Opening a link, skipping the update check")
    elif chrome_exe.exists() and is_chromium_running(chrome_exe):
        log("Chromium is running, skipping the update check")
    else:
        try:
            update_to_latest()
        except UpdateError as e:
            # An update problem must never stand between the user and the browser.
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
