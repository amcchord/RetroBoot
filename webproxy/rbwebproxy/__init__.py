"""RetroBoot Modern Web Helper: a translating web proxy for Mac OS 9 browsers.

The helper runs as its own unprivileged service (``retroboot-webproxy``),
started and stopped by hostd.  Mac OS reaches it through QEMU's user-mode
network: the service listens on host loopback, and while it runs the guest's
plain-HTTP and DNS traffic is redirected to it (``nft.py``).  It fetches http
and https upstream with certificate validation, rewrites https links to http,
and translates pages, images and text for Internet Explorer 5 and Netscape 4.
See ../README.md and docs/ARCHITECTURE.md section 11.
"""

__version__ = "1.0.0"
