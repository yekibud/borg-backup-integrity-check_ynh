Borg Backup Integrity gives you strong, inexpensive, recurring evidence that the Borg backups made by your YunoHost server (typically with the Borg app, borg_ynh) can actually be restored, *before* a real disaster.

For every check it:

* finds the newest backup generation in the Borg repository and builds a manifest of it (sizes and object counts per app and system part);
* compares that manifest with previous backups and warns prominently about suspicious growth, shrinkage, missing archives or empty database dumps;
* creates a fresh, cheap disposable server at your cloud provider (Hetzner Cloud or DigitalOcean), installs YunoHost and the Borg client on it;
* restores all core state normally (users, domains, app configuration, databases) and retrieves only the newest ~20 real user objects of every large data set (files, photos, mails, repositories...) instead of transferring terabytes;
* starts the restored services, checks HTTP endpoints and databases, and shows recognisable evidence (real file names, photo names, e-mail subjects and dates) taken from the backup, with an honest verification level for each;
* e-mails a clear plain-text report and destroys the temporary server.

A conventional complete restore (`full` mode) is available for occasional deeper tests.
