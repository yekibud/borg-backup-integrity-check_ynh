from datetime import timedelta

from tests.conftest import NOW, dir_items, make_item

from borg_backup_integrity_check.borg.layout import (
    BackupInfo,
    load_layout_from_dir,
    parse_backup_csv,
)
from borg_backup_integrity_check.borg.listing import DirectoryAggregates
from borg_backup_integrity_check.discovery.components import LargeRoot, components_from_layout
from borg_backup_integrity_check.discovery.large_data import (
    LargeDataDiscovery,
    is_db_dump_item,
    keep_small_files_in_large_roots,
)
from borg_backup_integrity_check.discovery.profiles import ProfileRegistry, SamplingProfile


def test_parse_backup_csv_handles_quotes():
    rows = parse_backup_csv(
        '"/var/www/app","apps/app/backup/var/www/app"\n"/home/yunohost.app/app","apps/app/backup/home/yunohost.app/app"\n'
    )
    assert (
        rows[0].source == "/var/www/app" and rows[1].dest == "apps/app/backup/home/yunohost.app/app"
    )


def test_backup_info_legacy_hooks_key_and_major_version():
    info = BackupInfo.from_dict(
        {"hooks": {"conf_ldap": {"paths": ["conf/ldap"]}}, "from_yunohost_version": "12.1.41.2"}
    )
    assert info.system == {"conf_ldap": {"paths": ["conf/ldap"]}} and info.yunohost_major == 12


def test_load_layout_from_dir(tmp_path, synthetic_app_layout):
    root = tmp_path / "layout"
    (root / "apps/filebox/settings").mkdir(parents=True)
    (root / "info.json").write_text(
        '{"created_at": 1, "size": 5, "size_details": {"system": {}, "apps": {"filebox": 5}}, "apps": {"filebox": {}}, "system": {}, "from_yunohost_version": "12.1.41"}'
    )
    (root / "backup.csv").write_text(
        '"/home/yunohost.app/filebox","apps/filebox/backup/home/yunohost.app/filebox"\n'
    )
    (root / "apps/filebox/settings/settings.yml").write_text(
        "id: filebox\ndata_dir: /home/yunohost.app/filebox\ndomain: example.org\n"
    )
    (root / "apps/filebox/settings/manifest.toml").write_text(
        'id = "filebox"\npackaging_format = 2\n[resources.data_dir]\n'
    )
    layout = load_layout_from_dir("auto_filebox-x", root)
    assert layout.apps["filebox"].data_dir == "/home/yunohost.app/filebox"
    assert layout.apps["filebox"].manifest_data_dir == "/home/yunohost.app/filebox"
    assert layout.apps["filebox"].packaging_format == 2
    assert (
        layout.source_for_dest("apps/filebox/backup/home/yunohost.app/filebox/alice/files/a.jpg")
        == "/home/yunohost.app/filebox/alice/files/a.jpg"
    )
    assert (
        layout.dest_for_source("/home/yunohost.app/filebox")
        == "apps/filebox/backup/home/yunohost.app/filebox"
    )


def test_directory_aggregates_and_tracking(synthetic_app_listing):
    agg = DirectoryAggregates.build(synthetic_app_listing, track=is_db_dump_item)
    root = agg.get("apps/filebox")
    assert root.files == agg.total_files
    data = agg.get("apps/filebox/backup/home/yunohost.app/filebox")
    assert data.files == 120 + 40 + 50 + 200 + 10 + 2
    assert agg.tracked_files == {"apps/filebox/backup/db.sql": 5 * 1024 * 1024}
    children = dict(agg.children("apps/filebox/backup/home/yunohost.app/filebox"))
    assert set(children) == {
        "apps/filebox/backup/home/yunohost.app/filebox/alice",
        "apps/filebox/backup/home/yunohost.app/filebox/bob",
        "apps/filebox/backup/home/yunohost.app/filebox/appdata_x1",
    }


def test_discovery_uses_data_dir_setting(synthetic_app_listing, synthetic_app_layout, archive_ref):
    comps = components_from_layout(archive_ref, "auto_filebox", synthetic_app_layout)
    assert [c.id for c in comps] == ["filebox"]
    agg = DirectoryAggregates.build(synthetic_app_listing, track=is_db_dump_item)
    comp = LargeDataDiscovery(ProfileRegistry([])).discover(comps[0], agg)
    assert len(comp.large_roots) == 1
    root = comp.large_roots[0]
    assert root.origin == "data_dir_setting"
    assert root.archive_path == "apps/filebox/backup/home/yunohost.app/filebox"
    assert root.live_path == "/home/yunohost.app/filebox"
    assert (
        comp.db_dump_paths == ["apps/filebox/backup/db.sql"]
        and comp.db_dump_size == 5 * 1024 * 1024
    )
    assert comp.core_files == 4 + 30 and comp.large_files == root.files
    assert comp.logical_size == agg.total_size


def test_discovery_heuristic_for_unknown_app_without_metadata(
    synthetic_app_listing, synthetic_app_layout, archive_ref
):
    synthetic_app_layout.apps["filebox"].settings = {"id": "filebox"}
    synthetic_app_layout.apps["filebox"].manifest = {"id": "filebox", "packaging_format": 2}
    comps = components_from_layout(archive_ref, "auto_filebox", synthetic_app_layout)
    agg = DirectoryAggregates.build(synthetic_app_listing)
    comp = LargeDataDiscovery(ProfileRegistry([])).discover(comps[0], agg)
    assert len(comp.large_roots) == 1
    assert comp.large_roots[0].origin == "size_heuristic"
    assert comp.large_roots[0].archive_path.startswith(
        "apps/filebox/backup/home/yunohost.app/filebox"
    )
    assert comp.large_roots[0].live_path.startswith("/home/yunohost.app/filebox")


def test_discovery_without_large_data_gives_no_roots(synthetic_app_layout, archive_ref):
    items = dir_items("apps/tiny/backup/var/www/tiny") + [
        make_item("apps/tiny/backup/var/www/tiny/index.php", 100),
        make_item("apps/tiny/backup/db.sql", 10),
    ]
    synthetic_app_layout.apps = {}
    synthetic_app_layout.info.apps = {"tiny": {}}
    comps = components_from_layout(archive_ref, "auto_tiny", synthetic_app_layout)
    comp = LargeDataDiscovery(ProfileRegistry([])).discover(
        comps[0], DirectoryAggregates.build(items)
    )
    assert comp.large_roots == [] and comp.core_files == 2


def test_system_data_components_are_large_by_definition(archive_ref, synthetic_app_layout):
    synthetic_app_layout.info.system = {
        "data_mail": {"paths": ["data/mail"]},
        "conf_ldap": {"paths": ["conf/ldap"]},
    }
    synthetic_app_layout.info.apps = {}
    synthetic_app_layout.apps = {}
    comps = {
        c.id: c for c in components_from_layout(archive_ref, "auto_data", synthetic_app_layout)
    }
    items = dir_items("data/mail/alice/cur") + [
        make_item(f"data/mail/alice/cur/{i}.eml", 500, NOW - timedelta(hours=i), user="vmail")
        for i in range(5)
    ]
    items += dir_items("conf/ldap") + [
        make_item("conf/ldap/dc=yunohost-dc=org.ldif", 9000, user="root")
    ]
    agg = DirectoryAggregates.build(items)
    mail = LargeDataDiscovery().discover(comps["data_mail"], agg)
    assert (
        mail.large_roots[0].origin == "system_data_part"
        and mail.large_roots[0].live_path == "/var/mail"
    )
    assert mail.large_files == 5 and mail.core_size == 0
    ldap = LargeDataDiscovery().discover(comps["conf_ldap"], agg)
    assert ldap.large_roots == [] and ldap.core_size == 9000


def test_profile_registry_matching(tmp_path):
    (tmp_path / "nextcloud.toml").write_text(
        'match_ids = ["nextcloud"]\nkind = "file"\n[large_data]\nroots = ["__DATA_DIR__"]\nexclude_dirs = ["appdata_*"]\n[verify]\nhttp_ok_codes = [200, 302]\n'
    )
    (tmp_path / "broken.toml").write_text("this is = not toml [")
    reg = ProfileRegistry([tmp_path])
    prof = reg.find("nextcloud")
    assert (
        isinstance(prof, SamplingProfile)
        and prof.kind == "file"
        and prof.exclude_dirs == ["appdata_*"]
        and prof.http_ok_codes == [200, 302]
    )
    assert reg.find("immich") is None and reg.find(None) is None


def test_layout_reads_main_domain(tmp_path):
    root = tmp_path / "layout"
    (root / "conf" / "ynh").mkdir(parents=True)
    (root / "info.json").write_text(
        '{"created_at": 1, "size": 5, "size_details": {"system": {"conf_ynh_settings": 5}, "apps": {}}, "apps": {}, "system": {"conf_ynh_settings": {}}, "from_yunohost_version": "12.1.41"}'
    )
    (root / "conf" / "ynh" / "current_host").write_text("example.org\n")
    layout = load_layout_from_dir("auto_conf-x", root)
    assert layout.main_domain == "example.org" and layout.system_parts == ["conf_ynh_settings"]


# ---------------------------------------------------------- small files inside large data
ROOT = "apps/immich/backup/home/yunohost.app/immich"


def _immich_component(synthetic_app_layout, archive_ref, listing_root=ROOT):
    synthetic_app_layout.apps = {}
    synthetic_app_layout.info.apps = {"immich": {}}
    comps = components_from_layout(archive_ref, "auto_immich", synthetic_app_layout)
    comp = comps[0]
    comp.large_roots = [
        LargeRoot(
            archive_path=listing_root, live_path="/home/yunohost.app/immich", origin="profile"
        )
    ]
    return comp


def test_app_plumbing_inside_a_large_root_is_restored_but_payload_is_not(
    synthetic_app_layout, archive_ref
):
    """The failure this rule exists for: immich's restore script chowns a file in its data dir."""
    comp = _immich_component(synthetic_app_layout, archive_ref)
    items = [
        make_item(f"{ROOT}/backups/restore_immich_db_backup.sh", 1200),
        make_item(f"{ROOT}/.env", 300),
        make_item(f"{ROOT}/library/meta/sidecar.json", 900),  # plumbing name, deeper
        make_item(f"{ROOT}/upload/thumbs/ab/cd/x.jpeg", 150_000),  # payload, deep
        make_item(f"{ROOT}/upload/upload/user/ab/big.jpg", 3_000_000),  # payload, big
        make_item(f"{ROOT}/alice/files/holiday.pdf", 200_000),  # user payload, not plumbing
        make_item(f"{ROOT}/backups/huge_dump.sql", 8 << 20),  # plumbing name but bulk-sized
    ]
    kept = keep_small_files_in_large_roots(comp, items)
    names = [p[len(ROOT) + 1 :] for p in comp.large_roots[0].keep_files]
    assert kept == 3
    assert names == [".env", "backups/restore_immich_db_backup.sh", "library/meta/sidecar.json"]
    assert comp.large_roots[0].keep_bytes == 2400


def test_user_payload_of_the_synthetic_app_is_never_kept(
    synthetic_app_listing, synthetic_app_layout, archive_ref
):
    comp = _immich_component(
        synthetic_app_layout, archive_ref, "apps/filebox/backup/home/yunohost.app/filebox"
    )
    assert keep_small_files_in_large_roots(comp, synthetic_app_listing) == 0


def test_keepers_are_bounded_when_a_root_is_full_of_small_files(synthetic_app_layout, archive_ref):
    comp = _immich_component(synthetic_app_layout, archive_ref)
    items = [make_item(f"{ROOT}/conf/n{i}.json", 200_000) for i in range(5000)]
    keep_small_files_in_large_roots(comp, items)
    root = comp.large_roots[0]
    assert len(root.keep_files) <= 2000 and root.keep_bytes <= 256 << 20


def test_kept_files_are_costed_as_disk_but_not_counted_as_backup_size(
    synthetic_app_layout, archive_ref
):
    """logical_size describes the backup; what we choose to restore must not inflate it."""
    from borg_backup_integrity_check.restore.planner import ComponentPlan, RestorePlan

    comp = _immich_component(synthetic_app_layout, archive_ref)
    comp.core_size = 1_000_000
    comp.large_roots[0].size = 59_000_000_000
    before = comp.logical_size
    keep_small_files_in_large_roots(comp, [make_item(f"{ROOT}/backups/restore.sh", 1200)])
    assert comp.logical_size == before  # the archive did not change

    plan = RestorePlan(mode="sampled")
    plan.apps.append(ComponentPlan(component=comp, restore_core=True, payload_mode="sampled"))
    with_keepers = plan.disk_estimate_bytes()
    comp.large_roots[0].keep_files, comp.large_roots[0].keep_bytes = [], 0
    assert with_keepers > plan.disk_estimate_bytes()  # but it does cost disk on the restore host
