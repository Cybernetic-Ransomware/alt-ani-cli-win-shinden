# Android wheels built by this project

Chaquopy's pip looks here (`--find-links`) for the `device` flavor only. Every wheel is built by the
`Android wheels` workflow (`.github/workflows/android-wheels.yml`, manual run) from a hash-checked source
archive, checked for 16 KB page alignment, and listed in `SHA256SUMS` with the workflow run that produced it.

| Wheel | Why it is not taken from an index |
|---|---|
| `cffi-2.1.1-cp314-cp314-android_24_arm64_v8a.whl` | `curl-cffi` 0.17.0 needs `cffi>=2.1.1`; neither PyPI nor Chaquopy publish an Android build of it |

Drop a wheel from here as soon as an index publishes an equivalent one.
