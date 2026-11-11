# Pi configurations

Configurations are public text and must not contain private content. `pi/base` reproduces the
original worker setup. Create a sibling folder for each tested setup; do not change a level in
place. `notes.md` records its hypothesis against its parent.

`configuration.json` supplies the Pi version, image, flags, tools, purpose, context window,
output token limit, thinking level, request and minute limits, system prompt and append files.
The system prompt is `default` or `system-prompt.md`; append files are paths under `append/`.
Only regular UTF-8 files are accepted. The runner accepts the supported Pi flags and built-in
tools only, and rejects unknown fields. Purpose and limit command-line options override defaults.

The manifest covers every file, including notes. Its configuration hash is SHA-256 of UTF-8
compact JSON encoding of sorted `[path, sha256]` pairs (ASCII escaped, no trailing newline).
The run records this manifest, declared Pi version, image ID and the fetched main commit. Merge
a configuration before queued tasks can use it; the runner archives a separate clone, never the
stack checkout. The runner selects a sibling with `--configuration pi/<name>`. Manual runs use `--configuration <dir>` or the installed checkout's base.
