"""`ops-judge triage <url>` / `ops-judge alerts watch|serve` / `ops-judge doctor` (also `python -m ops_judge`)."""

import sys

USAGE = """usage: ops-judge triage <GitHub Actions run URL> [options]   triage a failed CI run (see: ops-judge triage -h)
       ops-judge alerts watch|serve [options]                  triage alerts (see: ops-judge alerts -h)
       ops-judge doctor                                        check gh, claude, decision model"""


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) >= 2 else ""
    if cmd == "doctor":
        from ops_judge import doctor
        sys.exit(doctor.main())
    if cmd == "alerts":
        from ops_judge.alerts import watch
        watch.main(sys.argv[2:])
        return
    if cmd == "triage":
        from ops_judge import triage
        triage.main(sys.argv[2:])
        return
    sys.exit(USAGE)


if __name__ == "__main__":
    main()
