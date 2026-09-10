from datetime import timedelta

from tests.conftest import NOW, dir_items, make_item

from borg_backup_integrity_check.borg.listing import DirectoryAggregates
from borg_backup_integrity_check.discovery.components import components_from_layout
from borg_backup_integrity_check.discovery.large_data import LargeDataDiscovery
from borg_backup_integrity_check.discovery.profiles import ProfileRegistry
from borg_backup_integrity_check.sampling.sampler import GenericSampler, SamplingRules


def _component(listing, layout, ref):
    comp = components_from_layout(ref, "auto_filebox", layout)[0]
    return LargeDataDiscovery(ProfileRegistry([])).discover(
        comp, DirectoryAggregates.build(listing)
    )


def test_sampler_selects_newest_meaningful_objects(
    synthetic_app_listing, synthetic_app_layout, archive_ref
):
    comp = _component(synthetic_app_listing, synthetic_app_layout, archive_ref)
    samples = GenericSampler(SamplingRules(sample_size=20, spread_groups=False)).select(
        comp, synthetic_app_listing
    )
    assert len(samples) == 1
    objs = samples[0].objects
    assert len(objs) == 20
    paths = [o.relative_path for o in objs]
    assert not any(
        "appdata_x1" in p or "/cache/" in p or p.endswith(".hidden") or p.endswith("empty.txt")
        for p in paths
    )
    # Newest overall: bob's reports every 30 minutes and alice's photos every hour interleave.
    assert objs[0].mtime == NOW
    assert all(objs[i].sort_key >= objs[i + 1].sort_key for i in range(len(objs) - 1))
    assert samples[0].candidates_seen == 120 + 40 + 50
    assert samples[0].skipped_excluded >= 200 + 10 + 2
    assert objs[0].live_path.startswith("/home/yunohost.app/filebox/")


def test_sampler_spreads_across_groups(synthetic_app_listing, synthetic_app_layout, archive_ref):
    comp = _component(synthetic_app_listing, synthetic_app_layout, archive_ref)
    samples = GenericSampler(SamplingRules(sample_size=10, spread_groups=True)).select(
        comp, synthetic_app_listing
    )
    groups = {o.relative_path.split("/")[0] for o in samples[0].objects}
    assert groups == {"alice", "bob"}
    assert samples[0].groups_seen == 2
    assert len(samples[0].objects) == 10


def test_sampler_git_repositories_are_single_objects(synthetic_app_layout, archive_ref):
    root = "apps/filebox/backup/home/yunohost.app/filebox"
    listing = dir_items(f"{root}/alice/projects/tool/.git/refs/heads") + dir_items(
        f"{root}/alice/projects/tool/src"
    )
    listing += [
        make_item(f"{root}/alice/projects/tool/.git/HEAD", 23, NOW - timedelta(days=3)),
        make_item(f"{root}/alice/projects/tool/.git/config", 100, NOW - timedelta(days=3)),
        make_item(f"{root}/alice/projects/tool/.git/refs/heads/main", 41, NOW - timedelta(days=1)),
        make_item(f"{root}/alice/projects/tool/src/main.py", 4000, NOW - timedelta(days=2)),
        make_item(f"{root}/alice/notes.txt", 10, NOW),
    ]
    comp = _component(listing, synthetic_app_layout, archive_ref)
    objs = GenericSampler(SamplingRules(sample_size=5)).select(comp, listing)[0].objects
    kinds = {(o.kind, o.relative_path) for o in objs}
    assert ("git_repo", "alice/projects/tool") in kinds
    assert ("file", "alice/notes.txt") in kinds
    assert ("file", "alice/projects/tool/src/main.py") in kinds
    assert not any(".git/" in o.relative_path for o in objs)


def test_sampler_ignores_maildir_tmp_and_dovecot_indexes(synthetic_app_layout, archive_ref):
    synthetic_app_layout.info.system = {"data_mail": {"paths": ["data/mail"]}}
    synthetic_app_layout.info.apps = {}
    synthetic_app_layout.apps = {}
    comp = components_from_layout(archive_ref, "auto_data", synthetic_app_layout)[0]
    listing = (
        dir_items("data/mail/alice/cur")
        + dir_items("data/mail/alice/new")
        + dir_items("data/mail/alice/tmp")
        + dir_items("data/mail/alice/.Sent/cur")
    )
    listing += [
        make_item(f"data/mail/alice/cur/{i}.eml", 700, NOW - timedelta(hours=i)) for i in range(5)
    ]
    listing += [
        make_item("data/mail/alice/new/fresh.eml", 700, NOW),
        make_item("data/mail/alice/tmp/partial.eml", 700, NOW),
        make_item("data/mail/alice/dovecot.index.log", 5000, NOW),
        make_item("data/mail/alice/.Sent/cur/sent1.eml", 900, NOW - timedelta(minutes=5)),
    ]
    comp = LargeDataDiscovery().discover(comp, DirectoryAggregates.build(listing))
    objs = GenericSampler(SamplingRules(sample_size=20)).select(comp, listing)[0].objects
    rels = [o.relative_path for o in objs]
    assert "alice/new/fresh.eml" in rels and "alice/.Sent/cur/sent1.eml" in rels
    assert not any("tmp/" in r or "dovecot" in r for r in rels)
    assert len(objs) == 7


def test_sampler_newer_than_filter_and_serialisation(
    synthetic_app_listing, synthetic_app_layout, archive_ref
):
    comp = _component(synthetic_app_listing, synthetic_app_layout, archive_ref)
    rules = SamplingRules(sample_size=50, newer_than=NOW - timedelta(hours=2), spread_groups=False)
    objs = GenericSampler(rules).select(comp, synthetic_app_listing)[0].objects
    assert all(o.mtime >= NOW - timedelta(hours=2) for o in objs)
    d = objs[0].to_dict()
    assert d["archive_path"].startswith("apps/filebox/backup/") and d["kind"] == "file"
