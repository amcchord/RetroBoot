# Host your own image catalog

An image server publishes a small JSON catalog and downloadable machine archives. In RetroBoot 3.0.0, you can add up to eight additional catalogs. Each server has its own list of images and versions.

## Add a server

- **On RetroBoot** — open **Startup Disk → Install Another… → Download a Mac → Image Servers…**. Add your catalog’s full HTTPS URL and choose **Save**.
- **In the PC installer** — choose **Image Servers…** from **Choose Macs to Install**. The installer carries the saved URLs to the installed system. Choose **Install RetroBoot Only…** if you want to add a guest after installing.
- **In a paired browser** — open **Storage & Media → Image Servers**, enter one URL per line, then apply the settings.

Use addresses such as `https://macs.example.org/catalog.json` with a certificate trusted by RetroBoot. URLs accept no embedded credentials, query strings or fragments. Custom servers do not receive your RetroBoot downloads sign-in. Removing a server removes its listings. Macs already imported from it stay on your disk.

## Prepare an archive

Shut down the guest before taking a copy. Package a flat gzip-compressed tar archive with `config.json` and a raw `disk.img`. The repository’s packer writes the archive, checksum and numbers the catalog needs. For a Classic PowerPC machine:

```sh
python3 guest/build/catalog_archive.py ./machine ./system-9.thin.tar.gz \
  --url https://macs.example.org/images/system-9.thin.tar.gz \
  --json ./archive-facts.json
```

Use `--verify /path/to/a/scratch-directory` to extract the file through RetroBoot’s importer and compare the disk hash before publishing. Use `--classic` when you need a regular tar archive for a compatible older importer. The packer reads the machine source and writes the archive to the chosen output path.

A Quadra archive may include its own `Quadra800.rom` and `pram.img` from your authorized inputs. A PowerPC Mac OS X archive includes the matching `nvram.img` alongside the disk and configuration. Use `machineFamily: powerMacOSX` with the corresponding `guestOS` and `osBuild` in its listing. See the family markers used by [the importer](../../hostd/retroboot/catalog.py).

## Write the catalog

The top-level object has `"schemaVersion": 1` and a `machines` array. Copy the actual byte counts and SHA-256 from the packer. This is the shape of a Classic PowerPC entry, with example values:

```json
{
  "schemaVersion": 1,
  "machines": [
    {
      "id": "system-9-standard-v1",
      "name": "Mac OS 9.2.2",
      "summary": "Standard Mac OS installation.",
      "osVersion": "9.2.2",
      "machineFamily": "powerMacG4",
      "minimumAppVersion": "3.0.0",
      "archiveURL": "https://macs.example.org/images/system-9.thin.tar.gz",
      "archiveBytes": 1048576,
      "installedBytes": 20971522,
      "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
    }
  ]
}
```

For a thin archive, copy `archiveURL`, `archiveBytes` and `sha256` out of the packer’s `thinArchive` object into the primary fields. Include `diskCapacityBytes` and `requiredStorageBytes` if the packer supplies them. To support two representations of the same disk, use a regular archive in the primary fields and include the complete `thinArchive` object as the alternate.

Keep identifiers stable per edition and increase the final `-vN` when replacing an image. For example, `system-9-standard-v2` supersedes `system-9-standard-v1`. Leave older files at their old URLs if people already use them.

Serve the catalog and archives as ordinary HTTPS files. Enable byte ranges so an interrupted download can resume. Upload the archive first, then publish the catalog. Add the server on a test RetroBoot, import the image, and check that it reaches a responsive desktop. Only host software, ROMs and disk images you have permission to serve.
