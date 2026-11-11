# chromium-py-updater

A single-file launcher that keeps a portable Chromium install up to date on Windows.

On each start it asks GitHub for the latest Chromium release in the configured repository, downloads the chromium build from the release assets if it's not already on disk and launches `chrome.exe` against your profile directory.

If your build falls two or more major versions behind Chrome stable, Chromium-Py-Updater offers an up-to-date build to use in the meantime, and offers to switch back once yours catches up. See [When your build falls behind](#when-your-build-falls-behind).

## Requirements

- Windows
- Python 3.10 or newer (standard library only)
- For `.7z` releases: Windows 11, or 7-Zip installed

## Usage

First install [Python](https://www.python.org/downloads/windows/), then double click `Chromium.py`. On the first start it lists the Chromium builds it knows.

To switch to another build later, double click `choose-build.bat` (or run `python Chromium.py --choose-build`).

## Portable

All files live in the work directory next to `Chromium.py`:

| Path               | Contents                                               |
| ------------------ | ------------------------------------------------------ |
| `download/`        | the downloaded release archive (older ones are pruned) |
| `bin/`             | the extracted Chromium build                           |
| `profile/`         | your browser profile                                   |
| `temp/`            | disk cache, crash dumps                                |
| `latest.txt`       | which build and release is installed                   |
| `fallback.json`    | cached Chrome stable version and build list            |
| `launch.log`       | timestamped log of each run                            |
| `config.jsonc`     | generated on first run; yours to edit                  |
| `register.bat`     | register Chromium-Py-Updater as your default browser   |
| `choose-build.bat` | switch to another Chromium build                       |

## Configuration

On its first run the script creates its default configuration in `config.jsonc`. This file never gets overwritten, so your changes are safe across updates. The only lines Chromium-Py-Updater changes in it are `repo` and `asset_suffix`, when you select a new build. Any key you remove falls back to its built-in default. Delete it to get a fresh copy of the defaults.

Edit `config.jsonc` to your liking, for example:

```jsonc
{
  // Keep the profile outside the work dir
  "profile_dir": "{base_dir}/../chromium-profile",

  // Cache in the system temp directory instead
  "temp_dir": "{system_temp}/chromium",

  // Never offer another build, even when this one falls behind
  "offer_fallback": false,

  "chromium_args": [
    "--user-data-dir={profile_dir}",
    "--no-default-browser-check",
    "--disable-logging",
    "--disk-cache-dir={temp_dir}"
  ]
}
```

To track a build that isn't in the selection list, set `repo` and `asset_suffix`.

## Default browser

To use Chromium-Py-Updater as your default browser, double click `register.bat` and choose "Register".

This registers it with Windows for your user account (no admin rights needed) and opens the Default apps settings, where you can pick "Chromium-Py-Updater". Register again after moving the folder or reinstalling Python. To undo it, run `register.bat` and choose "Remove".

From a terminal, `python Chromium.py --register` and `--unregister` do the same.

## When your build falls behind

Volunteer-maintained builds sometimes fall behind Chrome stable for a while, and in that time they miss security fixes. On every start Chromium-Py-Updater compares your build with [Chrome stable](https://googlechromelabs.github.io/chrome-for-testing/).

When your build is two or more major versions behind, Chromium-Py-Updater offers alternative builds instead.

## Good to know

You can also run it with arguments. These are forwarded to Chromium after the configured ones, so `python Chromium.py --incognito https://example.com` works as you'd expect.

To add Widevine (needed for DRM-protected video) to ungoogled-chromium, see the [ungoogled-chromium FAQ](https://github.com/ungoogled-software/ungoogled-chromium-wiki/blob/master/faq.md#how-do-i-install-widevine-cdm); a `WidevineCdm` folder you put in `bin/` is kept on updates.

## Tests

`python tests\test_fallback.py` runs the tests on Windows. They fake GitHub and the downloads, so nothing goes online.

## License

MIT — see [LICENSE](LICENSE).
