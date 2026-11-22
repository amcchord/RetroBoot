/* RetroBoot original source, MIT. QA only: observe the live native PixMap. */
#include <Dialogs.h>
#include <Quickdraw.h>
#include <string.h>

int main(void)
{
    Str255 text;
    const char *message;
    short depth;
    InitGraf(&qd.thePort); InitFonts(); InitWindows(); InitMenus();
    TEInit(); InitDialogs(NULL); InitCursor();
    depth = (**(**GetMainDevice()).gdPMap).pixelSize;
    switch (depth) {
        case 1: message = "Current display: black and white (1-bit)."; break;
        case 4: message = "Current display: 16 colors (4-bit)."; break;
        case 8: message = "Current display: 256 colors (8-bit)."; break;
        case 16: message = "Current display: Thousands (16-bit)."; break;
        case 32: message = "Current display: Millions (32-bit)."; break;
        default: message = "Current display: another native pixel depth."; break;
    }
    text[0] = strlen(message);
    memcpy(text + 1, message, text[0]);
    ParamText(text, NULL, NULL, NULL);
    NoteAlert(128, NULL);
    return 0;
}
