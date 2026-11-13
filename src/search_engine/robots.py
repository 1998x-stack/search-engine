"""Conservative robots.txt parser with longest-match groups/rules."""
from __future__ import annotations
from dataclasses import dataclass
import re
from urllib.parse import urlsplit, unquote

@dataclass(frozen=True)
class RobotRule:
    allow: bool
    pattern: str

class RobotsPolicy:
    def __init__(self, groups: list[tuple[list[str], list[RobotRule]]]):
        self.groups = groups

    @classmethod
    def parse(cls, body: str) -> 'RobotsPolicy':
        groups: list[tuple[list[str], list[RobotRule]]] = []
        agents: list[str] = []
        rules: list[RobotRule] = []
        has_rules = False
        for line in body.splitlines():
            line = line.split('#', 1)[0].strip()
            if not line or ':' not in line:
                continue
            key, value = line.split(':', 1)
            key, value = key.strip().lower(), value.strip()
            if key == 'user-agent':
                if has_rules:
                    groups.append((agents, rules))
                    agents, rules, has_rules = [], [], False
                agents.append(value.lower())
            elif key in ('allow', 'disallow') and agents:
                has_rules = True
                if value:
                    rules.append(RobotRule(key == 'allow', value))
        if agents:
            groups.append((agents, rules))
        return cls(groups)

    def allowed(self, user_agent: str, url: str) -> bool:
        token = user_agent.split('/', 1)[0].lower().strip()
        best_specificity = -1
        selected: list[RobotRule] = []
        for agents, rules in self.groups:
            matches = [len(agent) for agent in agents if agent == '*' or (agent and agent in token)]
            if not matches:
                continue
            specificity = max(matches)
            if specificity > best_specificity:
                selected, best_specificity = list(rules), specificity
            elif specificity == best_specificity:
                selected.extend(rules)
        if not selected:
            return True
        parsed = urlsplit(url)
        target = unquote((parsed.path or '/') + ('?' + parsed.query if parsed.query else ''))
        match_len = -1
        decision = True
        for rule in selected:
            pattern = unquote(rule.pattern)
            regex = re.escape(pattern).replace(r'\*', '.*')
            if regex.endswith(r'\$'):
                regex = regex[:-2] + '$'
            if re.match(regex, target):
                specificity = len(pattern.replace('*', '').rstrip('$'))
                if specificity > match_len or (specificity == match_len and rule.allow):
                    match_len, decision = specificity, rule.allow
        return decision
