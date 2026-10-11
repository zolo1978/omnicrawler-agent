"""Fail-closed URL checks. Transport must pin validated IPs and check subrequests.

This module does not fetch URLs: preflight alone cannot prevent DNS rebinding.
"""

from ipaddress import ip_address
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser


class AccessDenied(ValueError):
    pass


class AccessPolicy:
    def __init__(self, allowed_hosts: set[str], user_agent: str = "OmniCrawler"):
        self.allowed_hosts = {
            host.lower().encode("idna").decode() for host in allowed_hosts
        }
        self.user_agent = user_agent

    def validate_url(self, url: str, addresses: list[str]) -> str:
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower().encode("idna").decode()
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise AccessDenied("Invalid scheme or embedded credentials")
            if any(char.isspace() for char in url) or host not in self.allowed_hosts:
                raise AccessDenied("Host is not allowed")
            if parsed.port not in {None, 80, 443}:
                raise AccessDenied("Port is not allowed")
            if not addresses:
                raise AccessDenied("DNS returned no addresses")
            try:
                literal = ip_address(host)
            except ValueError:
                literal = None
            if literal is not None and any(
                ip_address(address) != literal for address in addresses
            ):
                raise AccessDenied("Literal IP does not match supplied addresses")
            for address in addresses:
                ip = ip_address(address)
                if not ip.is_global or (
                    getattr(ip, "ipv4_mapped", None) is not None
                    and not ip.ipv4_mapped.is_global
                ):
                    raise AccessDenied("Non-public address")
            return host
        except (ValueError, UnicodeError) as exc:
            raise AccessDenied(str(exc)) from exc

    def robots_allowed(self, url: str, content: str | None) -> bool:
        if content is None:
            return False
        parser = RobotFileParser()
        parser.parse(content.splitlines())
        return parser.can_fetch(self.user_agent, url)
