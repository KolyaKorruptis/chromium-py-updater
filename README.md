# chromium-py-updater

A single-file launcher that keeps a portable [ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium-windows)
install up to date on Windows.

On each start it asks the GitHub API for the latest release, downloads the
`_windows_x64.zip` asset if it isn't the one already on disk, flattens it into `bin/`, and
launches `chrome.exe` against an isolated profile directory next to the script. The name of the
downloaded asset is recorded in `latest.txt`, so a run where nothing has changed skips the
download and extraction entirely and goes straight to launching.

## Requirements

- Windows
- Python 3.10 or newer
- No third-party packages — standard library only (`urllib`, `zipfile`, `ctypes`, `subprocess`)

## Usage

```
python Chromium.py [extra chromium args...]
```

Any arguments you pass are forwarded to Chromium after the configured ones, so
`python Chromium.py --incognito https://example.com` works as you'd expect.

Everything lives beside the script:

| Path         | Contents                                     |
| ------------ | -------------------------------------------- |
| `download/`  | the downloaded release zip (older ones are pruned) |
| `bin/`       | the extracted Chromium build                 |
| `profile/`   | the browser profile                          |
| `temp/`      | disk cache, crash dumps                      |
| `latest.txt` | name of the currently installed release asset |
| `launch.log` | timestamped log of each run                  |

## Configuration

Drop an optional `config.jsonc` next to the script to override any default. It is plain JSON
with whole-line `//` comments allowed. Missing keys fall back to the built-in defaults, and if
the file doesn't exist at all the defaults are used as-is.

```jsonc
{
  // Track a different build or architecture
  "repo": "ungoogled-software/ungoogled-chromium-windows",
  "asset_suffix": "_windows_x64.zip",

  // Keep the profile outside the repo folder
  "profile_dir": "{base_dir}/../chromium-profile",

  // Cache in the system temp directory instead
  "temp_dir": "{system_temp}/chromium",

  "chromium_args": [
    "--user-data-dir={profile_dir}",
    "--no-default-browser-check",
    "--disable-logging",
    "--disk-cache-dir={temp_dir}"
  ]
}
```

| Key             | Default                                              |
| --------------- | ---------------------------------------------------- |
| `repo`          | `ungoogled-software/ungoogled-chromium-windows`       |
| `asset_suffix`  | `_windows_x64.zip`                                    |
| `download_dir`  | `download`                                            |
| `extract_dir`   | `bin`                                                 |
| `profile_dir`   | `profile`                                             |
| `version_file`  | `latest.txt`                                          |
| `log_file`      | `launch.log`                                          |
| `temp_dir`      | `temp`                                                |
| `chromium_args` | see `load_config()` in `Chromium.py`                  |

Relative paths are resolved against the script's directory.

### Placeholders

Path settings accept `{base_dir}` and `{system_temp}`. Entries in `chromium_args` additionally
accept `{profile_dir}`, `{temp_dir}`, `{extract_dir}` and `{download_dir}`. An unknown
placeholder is reported as an error naming the setting it came from, rather than silently
producing a broken path.

## When Chromium is already running

If an update is available but the `chrome.exe` in `bin/` is still running, replacing it would
fail. In that case a message box appears offering to either launch the running version as-is
(skipping the update until next time) or cancel.

## License

MIT — see [LICENSE](LICENSE).
