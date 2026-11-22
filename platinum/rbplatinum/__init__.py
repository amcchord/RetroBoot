"""rbplatinum: build RetroBoot's Platinum asset bundle from the user's own Mac OS disk.

The bundle (fonts, icons, palettes, desktop pattern and Platinum chrome sprites)
is generated, never committed: everything Apple-derived stays on the user's
device or in private build output.  See ../README.md.
"""
__version__ = "6"   # 2: chrome sprites also as .rbi for the classic panel; 3: the padlock icon; 4: update, datetime;
                    # 5: metal/ (QuickTime Player 6's brushed-metal window); 6: Monitors' and Sound's
                    # icons at 40/64/16 px, the Keyboard icon, the plain slider thumb
