Required Setup for WebDAV Mounts
================================

davfs2
------

- Install [davfs2](https://savannah.nongnu.org/projects/davfs2/).  This code
  was initially tested against davfs2, version 1.6.1-1, installed via APT on
  Debian 12 (bookworm).

- Create a directory to use as the mount point; `tools/bench.sh` uses
  `/tmp/dandisets-fuse` by default, and this path will be used throughout the
  following instructions.

- Add the following to `/etc/davfs2/davfs2.conf`:

    ```
    [/tmp/dandisets-fuse]
    ask_auth 0
    follow_redirect 1
    ```

- Grant the user who will be running `dandisets-healthstatus` permission to run
  the following commands with `sudo` without entering a password:

    ```
    mount -t davfs https://webdav.dandiarchive.org /tmp/dandisets-fuse
    umount /tmp/dandisets-fuse
    ```

  This can be done by adding the following lines to `/etc/sudoers`, where
  `username` is replaced by the name of the user account:

    ```
    username ALL=(ALL:ALL) NOPASSWD: /usr/bin/mount -t davfs https\://webdav.dandiarchive.org /tmp/dandisets-fuse
    username ALL=(ALL:ALL) NOPASSWD: /usr/bin/umount /tmp/dandisets-fuse
    ```

- Ensure that the `dandidav` instance at webdav.dandiarchive.org is being run
  with the `--prefer-s3-redirects` option


<!--
webdavfs
--------

- Install [webdavfs](https://github.com/miquels/webdavfs).  The
  `mount.webdavfs` binary must be installed in `/sbin` so that `mount` can find
  it.

- Create a directory to use as the mount point; `tools/bench.sh` uses
  `/tmp/dandisets-fuse` by default, and this path will be used throughout the
  following instructions.

- Grant the user who will be running `dandisets-healthstatus` permission to run
  the following commands with `sudo` without entering a password:

    ```
    mount -t webdavfs -o allow_other https://webdav.dandiarchive.org /tmp/dandisets-fuse
    umount /tmp/dandisets-fuse
    ```

  This can be done by adding the following lines to `/etc/sudoers`, where
  `username` is replaced by the name of the user account:

    ```
    username ALL=(ALL:ALL) NOPASSWD: /usr/bin/mount -t webdavfs -o allow_other https\://webdav.dandiarchive.org /tmp/dandisets-fuse
    username ALL=(ALL:ALL) NOPASSWD: /usr/bin/umount /tmp/dandisets-fuse
    ```
-->
