import json
import time

from borg_backup_integrity_check.providers import catalog
from borg_backup_integrity_check.providers.base import MachineSize, Region
from borg_backup_integrity_check.providers.static import StaticHostProvider


class FakeCatalogProvider(StaticHostProvider):
    name = "fake"

    def __init__(self):
        super().__init__("127.0.0.1")
        self.calls = 0

    def list_regions(self):
        self.calls += 1
        return [Region("fsn1", "Falkenstein"), Region("gone", "Gone", available=False)]

    def list_sizes(self, region=None):
        return [
            MachineSize("cx22", "cx22", 2, 4096, 40, 0.006, 3.79),
            MachineSize("cax11", "cax11", 2, 4096, 40, architecture="arm"),
            MachineSize("old", "old", 1, 2048, 20, deprecated=True),
        ]


class FakeConfig:
    def credentials(self):
        return {}

    provider_settings = {}


def test_ynh_choices_yaml_keeps_current_and_extra():
    text = catalog.ynh_choices_yaml(
        [{"id": "fsn1", "name": "Falkenstein"}], current="nbg1", extra="auto"
    )
    assert text.splitlines()[0] == "choices:"
    assert '"auto": "Automatic (cheapest suitable)"' in text
    assert '"nbg1": "nbg1 (not found in the provider catalogue)"' in text
    assert text.splitlines()[-1] == 'value: "nbg1"'
    empty = catalog.ynh_choices_yaml([], current="fsn1")
    assert '"fsn1": "fsn1 (catalogue unavailable)"' in empty


def test_catalog_data_filters_and_caches(tmp_path, monkeypatch):
    provider = FakeCatalogProvider()
    monkeypatch.setattr(catalog, "create_provider", lambda name, creds, settings: provider)
    data = catalog.catalog_data(FakeConfig(), "fake", "all", "fsn1", cache_dir=tmp_path)
    assert [r["id"] for r in data["regions"]] == ["fsn1"]
    assert [s["id"] for s in data["sizes"]] == ["cx22"] and "3.79" in data["sizes"][0]["name"]
    cache = tmp_path / "catalog-fake-fsn1.json"
    assert cache.is_file() and oct(cache.stat().st_mode & 0o777) == "0o600"
    again = catalog.catalog_data(FakeConfig(), "fake", "regions", "fsn1", cache_dir=tmp_path)
    assert again["regions"] == data["regions"] and provider.calls == 1  # served from cache
    old = time.time() - catalog.CACHE_TTL - 10
    import os

    os.utime(cache, (old, old))
    catalog.catalog_data(FakeConfig(), "fake", "regions", "fsn1", cache_dir=tmp_path)
    assert provider.calls == 2
    assert json.loads(cache.read_text())["regions"]
