"""Remote screen for the web Screen page (ARCHITECTURE sections 4.6 and 7).

``hub.RemoteScreen`` streams the presenter's picture to paired browsers over a
WebSocket and routes their keyboard, pointer and pasted text back through the
presenter; ``keymap`` maps browser key codes, ``paste`` types text in Mac OS
Roman, ``encode`` makes lossless tiles and ``tap`` talks to the presenter.
"""
from .hub import MODES, RemoteScreen, Viewer

__all__ = ["MODES", "RemoteScreen", "Viewer"]
