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

Eval configurations may use `eval/<provider>/<model>` as their purpose. Each identifier starts
with a lower-case letter or digit, followed by lower-case letters, digits, `.`, `-` or `_`,
with 1–96 characters per identifier (198 total including separators). Ordinary purposes keep
`[a-z0-9][a-z0-9.-]{0,63}`. The single `eval/*` route sends the suffix to Vercel AI Gateway;
there is no model allowlist, and unknown IDs fail upstream.

Only tasks submitted with an eval mark may receive an eval-purpose token. Queued eval runs
use their configuration's purpose, with the existing request and minute limits; the reviewer
keeps its role route. Every gateway request for `eval/…` needs a live task token granting that
exact purpose, including from loopback and other client networks. Client keys cannot use it.
`pi/eval-deepseek-v4.1-flash` tests whether this route reaches the same model as `pi/base`'s
`task-worker`; it inherits all other settings and its 100-request, 30-minute limits.
