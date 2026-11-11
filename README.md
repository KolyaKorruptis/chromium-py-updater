# chromium-py-updater

A single-file launcher that keeps a portable Chromium install up to date on Windows.

You pick your preferred Chromium build from a list. Then on each start it checks and installs the latest release from that build, before launching Chromium against your `profile`. 

If your build falls two or more major versions behind, it will inform you and offer an up-to-date alternative. See [When your build falls behind](#when-your-build-falls-behind).

## Requirements

- Windows
- Python 3.10 or newer (standard library only)
- For `.7z` releases: Windows 11, or 7-Zip installed

## Usage

1. Install [Python](https://www.python.org/downloads/windows/)
2. Download [chromium-py-updater.zip](https://github.com/KolyaKorruptis/chromium-py-updater/releases/latest/download/chromium-py-updater.zip) and extract it to a folder of your choice
3. Double click `Chromium.py`

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

On its first run Chromium-Py-Updater creates its default configuration in `config.jsonc`. After that the only lines it will update are `repo` and `asset_suffix`, when you select a new build. Any key you remove from the config falls back to its built-in default. Delete `config.jsonc` to get a fresh copy of the defaults.

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

To track a build that isn't in the selection list, you can set `repo` and `asset_suffix` yourself.

## Default browser

To use Chromium-Py-Updater as your default browser, double click `register.bat` and choose "Register".

This registers it with Windows for your user account (no admin rights needed) and opens the Default apps settings, where you can pick "Chromium-Py-Updater". Register again after moving the folder or reinstalling Python. To undo it, run `register.bat` and choose "Remove".

From a terminal, `python Chromium.py --register` and `--unregister` do the same.

## When your build falls behind

Volunteer-maintained builds sometimes fall behind [Chrome stable](https://googlechromelabs.github.io/chrome-for-testing/) for a while and miss security fixes. 

When your build falls two or more major versions behind, Chromium-Py-Updater will offer to install an alternative build instead. Once your original build catches up, it offers to switch back.

## Good to know

To switch to another build at any time, just double click `choose-build.bat` (or run `python Chromium.py --choose-build`).

You can also run Chromium-Py-Updater with arguments. These are forwarded to Chromium after the configured ones, so `python Chromium.py --incognito https://example.com` works as you'd expect.

To add Widevine (for DRM-protected video) to ungoogled-chromium, install Google Chrome and copy the `WidevineCdm` folder from `C:\Program Files\Google\Chrome\Application\<Chrome version>\` into your profile as `profile\WidevineCdm\<Widevine version>\`, e.g. `profile\WidevineCdm\4.10.2934.0\`. The Widevine version is in the folder's `manifest.json`, and Chromium won't find Widevine without that version folder. Updates never touch your profile, so your copy stays. It isn't updated automatically, though, so repeat this if DRM video stops working. Some streaming services may refuse to play anyway (see [ungoogled-chromium-windows#247](https://github.com/ungoogled-software/ungoogled-chromium-windows/issues/247)). If you'd rather not deal with this, Marmaduke is also ungoogled and ships Widevine.

## Tests

`python tests\test_fallback.py` runs the tests on Windows. They fake GitHub and the downloads, so nothing goes online.

## License

MIT — see [LICENSE](LICENSE).
