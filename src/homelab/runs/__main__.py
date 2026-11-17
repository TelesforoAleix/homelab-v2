"""python -m homelab.runs [--healthcheck]."""

import sys

from homelab.runs import launcher


def main():
    if sys.argv[1:] == ["--healthcheck"]:
        try:
            return 0 if launcher.healthy() else 1
        except launcher.Refused:
            return 1
    from homelab.runs.runner import main as run

    return run()


if __name__ == "__main__":
    sys.exit(main())
