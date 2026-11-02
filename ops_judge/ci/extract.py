"""Turn a raw CI log into a short, clean excerpt plus the raw signals the facts are computed from."""

from __future__ import annotations

import re

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
GHA_TIMESTAMP = re.compile(r"^﻿?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z ?", re.MULTILINE)
GHA_JOB_PREFIX = re.compile(r"^[^\t\n]+\t[^\t\n]+\t", re.MULTILINE)  # `gh run view --log-failed`: job<TAB>step<TAB>

ERROR_MARKERS = re.compile(
    r"##\[error\]|Traceback \(most recent call last\)|^E\s{2,}|\bFAILED\b|\bFAIL\b|Error:|ERROR\b|"
    r"AssertionError|panic:|Exception\b|exit code [1-9]|npm ERR!|error\[E\d+\]|fatal:",
    re.MULTILINE,
)

INFRA_SIGNALS = {
    # Not bare "timeout": it also matches config names (sticky_wait_timeout) and plugins (pytest-timeouts).
    "timeout": r"\btimed out\b|\bTimeoutError\b|\bTimeoutException\b|deadline exceeded|ETIMEDOUT|Gateway Time-?out",
    "dns": r"getaddrinfo|ENOTFOUND|Name or service not known|Temporary failure in name resolution|EAI_AGAIN",
    "rate_limit": r"rate limit|\b429\b|Too Many Requests|API rate limit exceeded",
    "disk": r"No space left on device|ENOSPC|disk quota",
    "network": r"ECONNRESET|ECONNREFUSED|Connection reset|connection refused|network is unreachable|TLS handshake|\b50[234] (?:Bad Gateway|Service Unavailable|Gateway Time-?out)",
    "package_download": r"Could not resolve dependencies|failed to download|Could not download|Failed to fetch|ERR_PNPM_FETCH|npm ERR! (?:network|code E(?:AI|CONN))|pip.*(?:ReadTimeout|HTTPError)|Could not find a version",
    "oom": r"Out of memory|OOMKilled|JavaScript heap out of memory|\bMemoryError\b|Cannot allocate memory",
    # 137 = SIGKILL: OOM killer, spot reclaim or a cancelled runner. A hint, not a diagnosis.
    "sigkill": r"exit code 137\b",
    "runner": r"runner has received a shutdown signal|lost communication with the server|The operation was canceled",
    "spot_interruption": r"interrupted the EC2 Spot instance|spot instance (?:was )?(?:interrupted|reclaimed)|\bpreempted\b",
}
INFRA_RX = {k: re.compile(v, re.IGNORECASE | re.MULTILINE) for k, v in INFRA_SIGNALS.items()}
ENV_SIGNALS = re.compile(
    r"secret[s]? .*not (?:set|found)|environment variable .* (?:not set|missing)|KeyError: '[A-Z_]{3,}'|"
    r"command not found|No such file or directory|Permission denied|version .* not found|Unsupported engine",
    re.IGNORECASE,
)

STACK_PATTERNS = [
    re.compile(r'File "([^"]+)", line \d+'),                                 # Python
    re.compile(r"\(?((?:[\w.@-]+/)*[\w.@-]+\.(?:[jt]sx?|mjs|cjs)):\d+:\d+"),   # JS/TS
    re.compile(r"\b((?:[\w.-]+/)+[\w.-]+\.(?:py|go|rs|java|kt|rb|cs|cpp|cc|c|h|php|swift)):\d+"),  # generic path:line
    re.compile(r"\bat [\w.$]+\(([\w]+\.(?:java|kt)):\d+\)"),                 # JVM (file only)
]
NOISE_PATH = re.compile(r"site-packages|node_modules|/usr/lib|<frozen|internal/|/opt/hostedtoolcache")


def clean(log: str) -> str:
    log = ANSI.sub("", log).replace("\r\n", "\n")
    log = GHA_JOB_PREFIX.sub("", log) if log.count("\t") > log.count("\n") // 2 else log
    return GHA_TIMESTAMP.sub("", log)


FAILED_STEP_END = re.compile(r"##\[error\]Process completed with exit code")
ANNOTATION = re.compile(r"^##\[error\](?!Process completed with exit code)")


def failing_part(log: str) -> list[str]:
    """Cleaned lines up to the end of the first failed step. What follows is post-job cleanup
    (cache save, sanitizer reports, container teardown) and only adds noise."""
    lines = clean(log).split("\n")
    for i, line in enumerate(lines):
        if FAILED_STEP_END.search(line):
            return lines[:i + 1]
    return lines


def annotations(log: str) -> list[str]:
    """GitHub `##[error]` annotations other than the generic step-exit line: usually the best one-line summary."""
    return [line.strip()[len("##[error]"):] for line in failing_part(log) if ANNOTATION.search(line.strip())]


def error_excerpt(log: str, before: int = 25, after: int = 25, max_lines: int = 120) -> str:
    """Windows around error markers in the failing part, merged, capped. Later errors win when over
    budget (they are usually the final failure summary)."""
    lines = failing_part(log)
    hits = [i for i, line in enumerate(lines) if ERROR_MARKERS.search(line)]
    if not hits:
        return "\n".join(lines[-min(len(lines), before + after):])
    spans: list[list[int]] = []
    for i in hits:
        lo, hi = max(0, i - before), min(len(lines), i + after + 1)
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])
    chunks, used = [], 0
    for lo, hi in reversed(spans):
        take = min(hi - lo, max_lines - used)
        if take <= 0:
            break
        chunks.append(lines[hi - take:hi])
        used += take
    out = []
    for chunk in reversed(chunks):
        if out:
            out.append("...")
        out += chunk
    return "\n".join(out)


def stack_files(text: str) -> list[str]:
    found: list[str] = []
    for rx in STACK_PATTERNS:
        for m in rx.finditer(text):
            path = m.group(1).replace("\\", "/")
            if not NOISE_PATH.search(path) and path not in found:
                found.append(path)
    return found


def infra_signals(text: str) -> list[str]:
    return [k for k, rx in INFRA_RX.items() if rx.search(text)]


def env_signals(text: str) -> list[str]:
    return sorted({m.group(0)[:80] for m in ENV_SIGNALS.finditer(text)})


# Failing test names, per test runner. The name is what links this failure to the same test's history.
FAILING_TEST_PATTERNS = [
    re.compile(r"^(?:FAILED|ERROR)\s+(\S+::\S+)", re.MULTILINE),                    # pytest
    re.compile(r"^(?:FAIL|ERROR): \w+ \(([\w.]+)\)", re.MULTILINE),                 # unittest / cpython
    re.compile(r"^test (test_\w+) failed", re.MULTILINE),                            # cpython regrtest module
    re.compile(r"^\s*FAIL\s+(?:\S+\s+)?(\S+\.(?:[jt]sx?|mjs)(?: > [^\n]+)?)", re.MULTILINE),  # jest / vitest
    re.compile(r"^\s*\d+\) \[[^\]]+\] › ([^\n]+?)\s*─*$", re.MULTILINE),            # playwright
    re.compile(r"^test (\S+) \.\.\. FAILED", re.MULTILINE),                          # cargo
    re.compile(r"^---- (\S+) stdout ----", re.MULTILINE),                            # cargo
    re.compile(r"^\s*--- FAIL: (\S+)", re.MULTILINE),                                # go
]
DURATION = re.compile(r"\s*\(?\d+(?:\.\d+)?\s?m?s\)?$")


def failing_tests(text: str, limit: int = 20) -> list[str]:
    found: list[str] = []
    for rx in FAILING_TEST_PATTERNS:
        for m in rx.finditer(text):
            # Line numbers (playwright "spec.ts:12:5") move with every edit; drop them so history matches.
            name = re.sub(r":\d+:\d+", "", DURATION.sub("", m.group(1).strip()))[:200]
            if name and name not in found:
                found.append(name)
    return found[:limit]


FRAME_PATTERNS = [
    re.compile(r'File "([^"]+)", line (\d+)'),                                       # Python
    re.compile(r"((?:[\w.@-]+/)*[\w.@-]+\.(?:[jt]sx?|mjs|cjs|py|go|rs|java|kt|rb|cs|cpp|cc|c|h|php|swift)):(\d+)"),
]


def stack_frames(text: str) -> list[tuple[str, int]]:
    """(path, line) pairs from stack traces and compiler errors, library frames removed."""
    frames: list[tuple[str, int]] = []
    for rx in FRAME_PATTERNS:
        for m in rx.finditer(text):
            path = m.group(1).replace("\\", "/")
            if not NOISE_PATH.search(path) and (path, int(m.group(2))) not in frames:
                frames.append((path, int(m.group(2))))
    return frames
