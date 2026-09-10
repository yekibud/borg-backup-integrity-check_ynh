You need:

* Borg backups of this server, ideally created by the **Borg** app (borg_ynh): its repository, passphrase and SSH key are then discovered automatically;
* an API token (read & write) for **Hetzner Cloud** or **DigitalOcean**. A small server is created for each check and destroyed afterwards; a sampled check typically costs a few cents per run.

The disposable restore server needs to reach your Borg repository with the same SSH key/passphrase as this server (the key is copied to the temporary server only for the duration of the check). Repositories on local paths or private networks need a separate URL reachable from the cloud (asked during install).
