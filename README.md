# chromium-py-updater

A single-file launcher that keeps a portable Chromium install up to date on Windows.

On each start it asks GitHub for the latest Chromium release in the configured repository, 
downloads the chromium build from the release assets if it's not already on disk and launches `chrome.exe` against your profile directory. 

## Requirements

- Windows
- Python 3.10 or newer (standard library)

## Usage

Once Python is installed just double click `Chromium.py` script. 

You can also run it with arguments. These are forwarded to Chromium after the configured ones, 
so `python Chromium.py --incognito https://example.com` works as you'd expect.

## Portable

All files live in the work directory next to `Chromium.py`:

| Path         | Contents                                     |
| ------------ | -------------------------------------------- |
| `download/`  | the downloaded release zip (older ones are pruned) |
| `bin/`       | the extracted Chromium build                 |
| `profile/`   | your browser profile                          |
| `temp/`      | disk cache, crash dumps                      |
| `latest.txt` | name of the currently installed release asset |
| `launch.log` | timestamped log of each run                  |
| `config.jsonc` | generated on first run; yours to edit      |

## Configuration

On its first run the script creates its default configuration in `config.jsonc`. 
This file never gets overwritten, so your changes are safe across updates. 
Any key you remove falls back to its built-in default. 
Delete it to get a fresh copy of the defaults.

Edit `config.jsonc` to your liking, for example:

```jsonc
{
  // Track a different build or architecture
  "repo": "ungoogled-software/ungoogled-chromium-windows",
  "asset_suffix": "_windows_x64.zip",

  // Keep the profile outside the work dir
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

## Good to know

**If Chromium is running when an update is due**, you'll be asked what to do: skip the update and
launch straight away, or cancel and try again later. Skipping is safe — the update is simply
applied the next time you start with Chromium closed.

**If GitHub can't be reached**, the launcher starts the version you already have rather than
refusing to run, so a dropped connection never leaves you without a browser. The reason is
recorded in `launch.log`.

**You can find reliable build repositories** on https://chromiumbuilds.org/windows/

## License

MIT — see [LICENSE](LICENSE).
