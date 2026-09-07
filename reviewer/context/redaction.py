import re


class Redactor:
    def __init__(self, matches=()):
        self.secrets = {
            m["Secret"]: m.get("RuleID", "secret") for m in matches if m.get("Secret")
        }

    def text(self, value):
        for secret, rule in sorted(self.secrets.items(), key=lambda x: -len(x[0])):
            value = value.replace(secret, f"[REDACTED:{rule}]")
        # Defense in depth for credentials outside changed code, including requests.
        value = re.sub(
            r'(?i)(password|api[_-]?key|access[_-]?token|secret)([\s"\x27:=]+)([A-Za-z0-9_./+\-=]{12,})',
            r"\1\2[REDACTED:credential]",
            value,
        )
        return value

    def object(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.object(x) for x in value]
        if isinstance(value, dict):
            return {k: self.object(v) for k, v in value.items()}
        return value
