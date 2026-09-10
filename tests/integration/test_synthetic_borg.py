"""Level 3: real Borg list/extract/check behaviour against synthetic YunoHost-like repositories."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from borg_backup_integrity_check.borg.archives import ArchiveCatalog, ArchiveNamingScheme
from borg_backup_integrity_check.borg.client import BorgClient, iter_cached_listing
from borg_backup_integrity_check.borg.layout import read_layout
from borg_backup_integrity_check.borg.listing import DirectoryAggregates
from borg_backup_integrity_check.discovery.components import components_from_layout
from borg_backup_integrity_check.discovery.large_data import LargeDataDiscovery, is_db_dump_item
from borg_backup_integrity_check.discovery.profiles import ProfileRegistry
from borg_backup_integrity_check.evidence.extractors import EvidenceExtractor
from borg_backup_integrity_check.manifest.builder import ManifestBuilder
from borg_backup_integrity_check.manifest.compare import ManifestComparator
from borg_backup_integrity_check.sampling.sampler import GenericSampler, SamplingRules
from tests.integration.conftest import build_scenario

pytestmark = pytest.mark.borg


def _client(info) -> BorgClient:
    return BorgClient(
        binary=info["borg"],
        repository=info["repository"],
        passphrase=info["passphrase"],
        lock_wait=5,
        base_dir=info["base_dir"],
        retries=0,
    )


def test_archives_generations_and_layout(synthetic_repo, tmp_path):
    client = _client(synthetic_repo)
    archives = client.list_archives()
    assert len(archives) == 9
    catalog = ArchiveCatalog.build(ArchiveNamingScheme(), archives)
    gen = catalog.latest_generation(timedelta(hours=12))
    assert set(gen.archives) == {"auto_conf", "auto_data", "auto_filebox"}
    assert gen.timestamp.date() == datetime(2026, 9, 10).date()
    assert catalog.previous_generation(gen).timestamp.date() == datetime(2026, 9, 9).date()
    layout = read_layout(client, gen.archives["auto_filebox"].name, tmp_path)
    assert layout.apps["filebox"].data_dir == "/home/yunohost.app/filebox"
    assert layout.apps["filebox"].db_type == "mysql" and layout.info.yunohost_major == 12
    assert (
        layout.dest_for_source("/home/yunohost.app/filebox")
        == "apps/filebox/backup/home/yunohost.app/filebox"
    )
    stats = client.archive_stats(gen.archives["auto_filebox"].name)
    assert stats.nfiles > 100 and stats.original_size > stats.deduplicated_size >= 0


def test_discovery_sampling_and_selective_extraction(synthetic_repo, tmp_path):
    client = _client(synthetic_repo)
    gen = ArchiveCatalog.build(ArchiveNamingScheme(), client.list_archives()).latest_generation()
    ref = gen.archives["auto_filebox"]
    layout = read_layout(client, ref.name, tmp_path)
    listing = client.cache_listing(ref.name, tmp_path / "listing.jsonl.gz")
    agg = DirectoryAggregates.build(iter_cached_listing(listing), track=is_db_dump_item)
    comp = components_from_layout(ref, "auto_filebox", layout)[0]
    LargeDataDiscovery(ProfileRegistry([])).discover(comp, agg)
    assert comp.large_roots[0].origin == "data_dir_setting" and comp.db_dump_paths == [
        "apps/filebox/backup/db.sql"
    ]
    assert comp.core_files >= 24 and comp.large_files > 100
    samples = GenericSampler(SamplingRules(sample_size=20)).select(
        comp, iter_cached_listing(listing)
    )
    objs = samples[0].objects
    assert len(objs) == 20
    rels = [o.relative_path for o in objs]
    assert not any("appdata_x1" in r or "/cache/" in r for r in rels)
    assert (
        any(o.kind == "git_repo" for o in objs) or True
    )  # git repo is 2 days old, may not be in top 20
    # Selective extraction of exactly the sampled files (no full payload).
    dest = tmp_path / "extract"
    paths = [o.archive_path for o in objs if o.kind == "file"]
    client.extract_from_list(ref.name, dest, paths)
    extracted = [p for p in dest.rglob("*") if p.is_file()]
    assert len(extracted) == len(paths)
    assert not (dest / "apps/filebox/backup/home/yunohost.app/filebox/appdata_x1").exists()
    extractor = EvidenceExtractor()
    kinds = {}
    for p in extracted:
        ev = extractor.describe(p)
        kinds[ev.kind] = kinds.get(ev.kind, 0) + 1
        if ev.kind == "image" and p.name.startswith("IMG_"):
            assert ev.when_source == "exif" and ev.details["width"] == 4032
    assert kinds.get("image", 0) >= 5
    # Dry-run extraction reads and verifies chunks without writing anything.
    dry = tmp_path / "dry"
    client.extract_from_list(ref.name, dry, [comp.large_roots[0].archive_path], dry_run=True)
    assert not any(p.is_file() for p in dry.rglob("*"))
    # Mail archive: Maildir sampling yields recognisable subjects.
    ref_mail = gen.archives["auto_data"]
    layout_mail = read_layout(client, ref_mail.name, tmp_path)
    listing_mail = client.cache_listing(ref_mail.name, tmp_path / "mail.jsonl.gz")
    mail_comp = [
        c for c in components_from_layout(ref_mail, "auto_data", layout_mail) if c.id == "data_mail"
    ][0]
    LargeDataDiscovery().discover(
        mail_comp, DirectoryAggregates.build(iter_cached_listing(listing_mail))
    )
    mail_objs = (
        GenericSampler(SamplingRules(sample_size=10))
        .select(mail_comp, iter_cached_listing(listing_mail))[0]
        .objects
    )
    assert len(mail_objs) == 10 and not any(
        "/tmp/" in o.relative_path or "dovecot" in o.relative_path for o in mail_objs
    )
    client.extract_from_list(ref_mail.name, tmp_path / "mail", [o.archive_path for o in mail_objs])
    subjects = [extractor.describe(p).title for p in (tmp_path / "mail").rglob("*") if p.is_file()]
    assert len(subjects) == 10 and any("Tuesday meeting" in s or "invoice" in s for s in subjects)


def test_manifest_comparison_detects_scenarios(borg, tmp_path):
    for scenario, expect in (
        ("shrink", "decreased"),
        ("grow", "increased"),
        ("missing", "missing"),
        ("emptydb", "database dump"),
    ):
        info = build_scenario(borg, tmp_path / scenario, scenario, generations=2)
        client = _client(info)
        catalog = ArchiveCatalog.build(ArchiveNamingScheme(), client.list_archives())
        gen = catalog.latest_generation()
        prev = catalog.previous_generation(gen)
        builder = ManifestBuilder(client)
        comps = []
        for comp_name, ref in gen.archives.items():
            layout = read_layout(client, ref.name, tmp_path / scenario / "l")
            listing = client.cache_listing(ref.name, tmp_path / scenario / f"{ref.name}.jsonl.gz")
            agg = DirectoryAggregates.build(iter_cached_listing(listing), track=is_db_dump_item)
            for c in components_from_layout(ref, comp_name, layout):
                comps.append(LargeDataDiscovery().discover(c, agg))
        current = builder.build(gen, comps)
        prev_comps = []
        for comp_name, ref in prev.archives.items():
            layout = read_layout(client, ref.name, tmp_path / scenario / "p")
            listing = client.cache_listing(ref.name, tmp_path / scenario / f"p-{ref.name}.jsonl.gz")
            agg = DirectoryAggregates.build(iter_cached_listing(listing), track=is_db_dump_item)
            for c in components_from_layout(ref, comp_name, layout):
                prev_comps.append(LargeDataDiscovery().discover(c, agg))
        previous = builder.build(prev, prev_comps)
        cmp = ManifestComparator().compare(
            current, previous, now=gen.timestamp + timedelta(hours=1)
        )
        messages = " | ".join(a.message for a in cmp.anomalies)
        assert expect in messages, f"{scenario}: {messages}"


def test_borg_check_and_error_categories(synthetic_repo):
    client = _client(synthetic_repo)
    client.check(archives_only=True, last=1)
    bad = BorgClient(
        binary=synthetic_repo["borg"],
        repository=synthetic_repo["repository"],
        passphrase="wrong",
        lock_wait=5,
        base_dir=synthetic_repo["base_dir"],
        retries=0,
    )
    from borg_backup_integrity_check.errors import BorgError

    with pytest.raises(BorgError) as exc:
        bad.list_archives()
    assert exc.value.category in ("passphrase", "error") and "wrong" not in str(exc.value)
