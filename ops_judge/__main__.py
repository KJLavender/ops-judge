"""`ops-judge triage <url>` / `ops-judge doctor` (also runnable as `python -m ops_judge`)."""

import sys

from ops_judge import triage

USAGE = """usage: ops-judge triage <GitHub Actions run URL> [options]   (see: ops-judge triage -h)
       ops-judge doctor                                       (check gh, claude, decision model)"""


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "doctor":
        from ops_judge import doctor
        sys.exit(doctor.main())
    if len(sys.argv) < 2 or sys.argv[1] != "triage":
        sys.exit(USAGE)
    triage.main(sys.argv[2:])


if __name__ == "__main__":
    main()
