"""Error-template index: "has this error template shown up on main recently?" is a strong flaky/infra signal.

Uses Drain3 when installed; otherwise falls back to masking variable tokens (numbers, hex, paths, quotes).
"""

from __future__ import annotations

import re

MASKS = [
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<HEX>"),
    (re.compile(r"\b\d+(\.\d+)*(ms|s|m)?\b"), "<NUM>"),
    (re.compile(r"(/[\w.-]+){2,}"), "<PATH>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<STR>"),
    (re.compile(r"\s+"), " "),
]


def mask(line: str) -> str:
    for rx, token in MASKS:
        line = rx.sub(token, line)
    return line.strip()


class TemplateIndex:
    def __init__(self, use_drain: bool = True):
        self.miner = None
        if use_drain:
            try:
                from drain3 import TemplateMiner
                from drain3.template_miner_config import TemplateMinerConfig

                cfg = TemplateMinerConfig()
                cfg.profiling_enabled = False
                self.miner = TemplateMiner(config=cfg)
            except ImportError:
                pass
        self.seen: set[str] = set()

    def template(self, line: str) -> str:
        if self.miner is None:
            return mask(line)
        match = self.miner.match(mask(line))
        return match.get_template() if match else mask(line)

    def add(self, line: str) -> str:
        if self.miner is not None:
            t = self.miner.add_log_message(mask(line))["template_mined"]
        else:
            t = mask(line)
        self.seen.add(t)
        return t

    def contains(self, line: str) -> bool:
        return self.template(line) in self.seen
