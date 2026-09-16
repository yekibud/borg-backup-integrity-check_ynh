"""RestorePlan: what to restore, in which order, and how big the disposable host must be."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..discovery.components import Component
from ..sampling.sampler import RootSample

BASE_DISK_GB = 8  # Debian + YunoHost + borg build + logs
MIN_HEADROOM_GB = 3
SYSTEM_PARTS_SKIPPED_BY_DEFAULT = ("conf_manually_modified_files",)


@dataclass
class ComponentPlan:
    component: Component
    restore_core: bool = True
    payload_mode: str = "sampled"  # sampled | full | none
    samples: list[RootSample] = field(default_factory=list)
    reason: str = ""

    @property
    def sampled_bytes(self) -> int:
        return sum(o.size for s in self.samples for o in s.objects)


@dataclass
class RestorePlan:
    mode: str
    system_conf: list[ComponentPlan] = field(default_factory=list)
    apps: list[ComponentPlan] = field(default_factory=list)
    system_data: list[ComponentPlan] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    min_memory_mb: int = 4096

    @property
    def all_plans(self) -> list[ComponentPlan]:
        return self.system_conf + self.apps + self.system_data

    @property
    def main_domain_available(self) -> bool:
        return any(p.component.id == "conf_ynh_settings" for p in self.system_conf)

    def disk_estimate_bytes(self) -> int:
        total = BASE_DISK_GB * 1e9
        for plan in self.all_plans:
            comp = plan.component
            if plan.restore_core:
                kept = sum(root.keep_bytes for root in comp.large_roots)
                total += (
                    comp.core_size + kept
                ) * 3 + comp.db_dump_size * 2  # tar + extracted work dir + restored copy
            if plan.payload_mode == "full":
                total += comp.large_size * 1.2
            elif plan.payload_mode == "sampled":
                total += plan.sampled_bytes * 2.5
        return int(total * 1.15)

    def required_disk_gb(self) -> int:
        return int(math.ceil(self.disk_estimate_bytes() / 1e9)) + MIN_HEADROOM_GB

    def summary(self) -> str:
        parts = [
            f"{len(self.system_conf)} system config part(s)",
            f"{len(self.apps)} app(s)",
            f"{len(self.system_data)} system data part(s)",
        ]
        return ", ".join(parts) + f"; estimated disk need {self.required_disk_gb()} GB"


def build_plan(
    components: list[Component],
    samples_by_component: dict[str, list[RootSample]],
    mode: str,
    selected: list[str],
    never_restore: set[str],
    skip_system_parts: tuple[str, ...] = SYSTEM_PARTS_SKIPPED_BY_DEFAULT,
    min_memory_mb: int = 4096,
    backup_tooling: frozenset[str] = frozenset({"borg", "borgserver", "borgwarehouse"}),
) -> RestorePlan:
    plan = RestorePlan(mode=mode, min_memory_mb=min_memory_mb)
    select_all = "all" in selected
    for comp in components:
        if comp.is_app:
            manifest_id = comp.app.manifest_id if comp.app else comp.id
            if comp.id in never_restore or manifest_id in never_restore:
                # Two different reasons land here, and the report should not conflate them.
                if comp.id in backup_tooling or manifest_id in backup_tooling:
                    reason = (
                        "backup tooling: restoring it on the test server would hand it your "
                        "repository credentials and cloud token"
                    )
                else:
                    reason = "excluded by configuration ('Apps never restored on the test server')"
                plan.skipped.append((comp.id, reason))
                continue
            if not select_all and comp.id not in selected and manifest_id not in selected:
                plan.skipped.append((comp.id, "not selected"))
                continue
            if comp.app and not comp.app.settings and not comp.app.manifest:
                plan.skipped.append((comp.id, "no app settings/manifest in archive"))
                continue
        elif comp.kind == "system_conf":
            if comp.id in skip_system_parts:
                plan.skipped.append((comp.id, "skipped by policy"))
                continue
            if not select_all and comp.id not in selected and "system" not in selected:
                plan.skipped.append((comp.id, "not selected"))
                continue
        elif (
            comp.kind == "system_data"
            and not select_all
            and comp.id not in selected
            and "system" not in selected
        ):
            plan.skipped.append((comp.id, "not selected"))
            continue
        cp = ComponentPlan(component=comp, samples=samples_by_component.get(comp.id, []))
        if not comp.large_roots:
            cp.payload_mode = "none"
        elif mode == "full":
            cp.payload_mode = "full"
        else:
            cp.payload_mode = "sampled"
        if comp.kind == "system_conf":
            plan.system_conf.append(cp)
        elif comp.kind == "system_data":
            cp.restore_core = False  # the whole part is payload; nothing else to restore
            plan.system_data.append(cp)
        else:
            plan.apps.append(cp)
    order = {"conf_ldap": 0, "conf_ynh_settings": 1, "conf_ynh_certs": 2}
    plan.system_conf.sort(key=lambda p: order.get(p.component.id, 9))
    return plan
