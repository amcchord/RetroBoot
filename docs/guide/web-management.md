# Web management

RetroBoot serves its own control panel. Open it in a browser on the same network to see the running Mac, manage its settings, add disk images, or install an update.

![A paired browser shows the running Mac, its live Finder screen, and the startup timeline](../images/web-overview.jpg)

## Pair a browser

1. On the RetroBoot computer, press **Ctrl+Alt+Delete** and open **Network & Web Access…**. The same panel is available during Setup.
2. Open the address shown there. RetroBoot uses its own HTTPS certificate. Compare its SHA-256 fingerprint against the one shown on the RetroBoot screen.
3. Enter the six-digit code and a name for the browser. Use **Paired Devices** to remove a browser when it no longer needs access.

![RetroBoot's local controls over the running Mac desktop](../images/retroboot-panel.png)

## Manage Macs and image servers

Open **Storage & Media** to see installed Macs, browse catalog downloads, or import an archive. Under **Image Servers**, enter one HTTPS catalog URL per line and apply the settings. [Host your own catalog](image-servers.md)

![Storage & Media in a paired browser shows installed Macs and catalog imports](../images/web-storage.jpg)
