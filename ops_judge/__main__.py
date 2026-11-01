"""`python -m ops_judge triage <url>` (also installed as the `ops-judge` command)."""

import sys

from ops_judge import triage


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "triage":
        sys.exit("usage: ops-judge triage <GitHub Actions run URL> [options]   (see: ops-judge triage -h)")
    triage.main(sys.argv[2:])


if __name__ == "__main__":
    main()
