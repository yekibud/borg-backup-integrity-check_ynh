"""ApplicationHealthChecker: services, HTTP, database and application-level sample verification."""

from __future__ import annotations

from ..discovery.components import Component
from ..discovery.profiles import SamplingProfile
from ..logging_setup import get_logger
from ..report.models import FAIL, PASS, SKIPPED, WARN, ComponentReport, VerificationLevel
from .agent import HostAgent

log = get_logger("health")

HTTP_OK = set(range(200, 400))
HTTP_AUTH = {401, 403}


class ApplicationHealthChecker:
    def __init__(self, agent: HostAgent) -> None:
        self.agent = agent

    def check_app(
        self, comp: Component, report: ComponentReport, profile: SamplingProfile | None = None
    ) -> None:
        app = comp.app
        settings = {}
        live = self.agent.call("app-settings", args=["--app", comp.id], timeout=120)
        if live.get("ok"):
            settings = live.get("settings") or {}
        domain = settings.get("domain") or (app.domain if app else None)
        path = settings.get("path") or (app.path if app else None) or "/"
        db_name = settings.get("db_name") or (app.db_name if app else None)
        db_type = app.db_type if app else None
        basenames = sorted(
            {s.evidence.title for s in report.samples if s.ok and s.evidence.kind != "git_repo"}
        )
        spec = {
            "app": comp.id,
            "services": list(profile.services) if profile and profile.services else [],
            "domain": domain,
            "path": path,
            "db_name": db_name,
            "db_type": db_type,
            "basenames": basenames[:200],
            "urls": self._profile_urls(profile, domain, path, report),
        }
        result = self.agent.call("health", spec=spec, timeout=3600)
        self._apply_services(result, report)
        self._apply_http(result, report, profile)
        self._apply_db(result, report, db_name)
        self._apply_db_refs(result, report)

    # ----------------------------------------------------------- services
    @staticmethod
    def _apply_services(result: dict, report: ComponentReport) -> None:
        registered = result.get("registered_services") or []
        explicit = result.get("services") or []
        if not registered and not explicit:
            report.add_check(
                "Service health", SKIPPED, "no dedicated service (web app served by nginx/php)"
            )
            return
        failing = [s["name"] for s in registered if s.get("status") not in ("running", None)] + [
            s["name"] for s in explicit if s.get("state") != "active"
        ]
        names = sorted({s["name"] for s in registered} | {s["name"] for s in explicit})
        if failing:
            report.add_check(
                "Service health",
                FAIL,
                f"not running: {', '.join(sorted(set(failing)))}",
                VerificationLevel.OBJECT_EXTRACTED,
            )
        else:
            report.add_check(
                "Service health", PASS, ", ".join(names), VerificationLevel.SERVICE_RUNNING
            )

    @staticmethod
    def _apply_http(result: dict, report: ComponentReport, profile: SamplingProfile | None) -> None:
        http = result.get("http")
        if not http:
            report.add_check("HTTP health", SKIPPED, "no domain/path known")
            return
        code = http.get("code", 0)
        ok_codes = set(profile.http_ok_codes) if profile and profile.http_ok_codes else HTTP_OK
        if code in ok_codes:
            report.add_check(
                "HTTP health", PASS, f"{code} {http['url']}", VerificationLevel.HTTP_RESPONDING
            )
        elif code in HTTP_AUTH:
            report.add_check(
                "HTTP health",
                PASS,
                f"{code} (authentication required) {http['url']}",
                VerificationLevel.HTTP_RESPONDING,
            )
        elif code == 404:
            report.add_check(
                "HTTP health", WARN, f"404 {http['url']}", VerificationLevel.HTTP_RESPONDING
            )
        else:
            report.add_check("HTTP health", FAIL, f"HTTP {code or 'unreachable'} {http['url']}")
        sso = result.get("sso")
        if sso:
            report.add_check(
                "Login endpoint",
                PASS if sso.get("code") in HTTP_OK else WARN,
                f"{sso.get('code')} {sso.get('url')}",
                VerificationLevel.HTTP_RESPONDING
                if sso.get("code") in HTTP_OK
                else VerificationLevel.NOT_CHECKED,
            )
        for extra in result.get("extra_urls", []) or []:
            if extra.get("code") in HTTP_OK:
                report.notes.append(
                    f"application URL reachable: {extra.get('code')} {extra.get('url')}"
                )

    @staticmethod
    def _apply_db(result: dict, report: ComponentReport, db_name: str | None) -> None:
        db = result.get("db")
        if not db_name:
            return
        if not db:
            report.add_check("Database restore", SKIPPED, "database type unknown")
            return
        if db.get("ok") and db.get("tables", 0) > 0:
            report.add_check(
                "Database restore",
                PASS,
                f"{db['tables']} tables in {db_name}",
                VerificationLevel.OBJECT_EXTRACTED,
            )
        elif db.get("ok"):
            report.add_check("Database restore", FAIL, f"database {db_name} has no tables")
        else:
            report.add_check(
                "Database restore", FAIL, (db.get("error") or "database not reachable")[:200]
            )

    @staticmethod
    def _apply_db_refs(result: dict, report: ComponentReport) -> None:
        refs = set(result.get("db_refs") or [])
        if not refs:
            return
        count = 0
        for sample in report.samples:
            if (
                sample.ok
                and sample.evidence.title in refs
                and sample.level < VerificationLevel.REFERENCED_BY_APPLICATION
            ):
                sample.level = VerificationLevel.REFERENCED_BY_APPLICATION
                count += 1
        if count:
            report.notes.append(
                f"{count} sampled object name(s) are referenced in the restored application database"
            )

    @staticmethod
    def _profile_urls(
        profile: SamplingProfile | None, domain: str | None, path: str, report: ComponentReport
    ) -> list[str]:
        if not profile or not profile.http_object_path or not domain:
            return []
        urls = []
        base = f"https://{domain}{path.rstrip('/')}"
        for sample in report.samples[:5]:
            rel = sample.evidence.details.get("relative_path") or sample.evidence.title
            urls.append(base + profile.http_object_path.replace("__OBJECT__", rel))
        return urls

    # ---------------------------------------------------------------- mail
    def verify_mail(self, report: ComponentReport) -> None:
        objects = []
        for sample in report.samples:
            if not sample.ok or sample.evidence.kind != "email":
                continue
            user = _maildir_user(sample.evidence.path or "")
            message_id = sample.evidence.details.get("message_id")
            if user and message_id:
                objects.append(
                    {"archive_path": sample.archive_path, "user": user, "message_id": message_id}
                )
        if not objects:
            return
        result = self.agent.call("mail-verify", spec={"objects": objects}, timeout=1800)
        verified = {o["archive_path"] for o in result.get("objects", []) if o.get("verified")}
        for sample in report.samples:
            if sample.archive_path in verified:
                sample.level = VerificationLevel.VERIFIED_THROUGH_APPLICATION
        if verified:
            report.add_check(
                "Mail server access",
                PASS,
                f"{len(verified)}/{len(objects)} sampled messages found through Dovecot",
                VerificationLevel.VERIFIED_THROUGH_APPLICATION,
            )
        elif result.get("error"):
            report.add_check("Mail server access", SKIPPED, result["error"])
        else:
            report.add_check(
                "Mail server access",
                WARN,
                f"0/{len(objects)} sampled messages visible through Dovecot",
            )


def _maildir_user(relative_path: str) -> str | None:
    """``alice/cur/123`` or ``/var/mail/alice/...`` -> ``alice``."""
    parts = [p for p in relative_path.split("/") if p]
    if parts and parts[0] == "var" and len(parts) > 2 and parts[1] == "mail":
        return parts[2]
    return parts[0] if parts else None
