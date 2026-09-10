# Sampling profiles (optional adapters)

Profiles are small declarative TOML files that *augment* the generic discovery
for a specific upstream application. They are never required: unknown apps
are handled from their YunoHost metadata (`data_dir`, backup.csv, manifest) and
a size heuristic.

```toml
match_ids = ["nextcloud"]        # manifest ids (fnmatch patterns)
kind = "file"                    # file | media | mail | repo (report wording only)

[large_data]
roots = ["__DATA_DIR__"]         # extra large roots (__DATA_DIR__, __INSTALL_DIR__, __APP__ placeholders)
exclude_dirs = ["appdata_*"]     # additional directory names skipped when sampling
exclude_files = ["*.ocTransferId*"]
include_globs = ["*/files/**"]   # when set, only paths matching one glob are candidates

[verify]
services = []                    # extra systemd units to check
http_ok_codes = [200, 302]       # accepted codes for the app's main URL
http_object_path = "/remote.php/dav/files/__OBJECT__"   # optional per-object URL template (reported when reachable)
```
