"""Software Update: signed bundles, A/B system slots, trial boots and rollback (ARCHITECTURE 13).

``slotenv``, ``bundle``, ``tree`` and ``writer`` use only the standard library and
python3-cryptography and import nothing from the rest of hostd, so the lab bootstrap
(``python3 -m <pkg> apply``) and the slot guard run them on their own.  ``manager``
is hostd's side (jobs, panel and web).
"""
