"""RetroBoot classic Mac application media tools (host side, Python standard library).

Converts classic Mac software distributions -- StuffIt archives, Disk Copy
4.2 and NDIF (Disk Copy 6) floppy images, HFS/HFS+ volumes and CD images --
into a canonical tree that keeps every file's data fork, resource fork and
Finder information (``tree``), which ``rbguestprep.install.install_tree``
copies into a stopped guest disk.
"""

__version__ = "1"
