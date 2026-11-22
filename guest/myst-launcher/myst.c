/* Original RetroBoot code (MIT). Original game and media remain unchanged.
 * Install beside " Myst" and "Myst CD.iso"; expose this app in Start > Games.
 * Finder opens the image with its original registered Virtual CD handler.
 */
#include <AppleEvents.h>
#include <Aliases.h>
#include <Dialogs.h>
#include <Events.h>
#include <Files.h>
#include <FinderRegistry.h>
#include <Processes.h>
#include <Quickdraw.h>
#include <string.h>

static Boolean quitting;

static void pascal_string(Str255 out, const char *text)
{
    size_t n = strlen(text);
    if (n > 255) n = 255;
    out[0] = (unsigned char)n;
    memcpy(out + 1, text, n);
}

static pascal OSErr quit_event(const AppleEvent *event, AppleEvent *reply, long ref)
{
    (void)event; (void)reply; (void)ref;
    quitting = true;
    return noErr;
}

static void yield(void)
{
    EventRecord event;
    if (WaitNextEvent(everyEvent, &event, 15, NULL) && event.what == kHighLevelEvent)
        AEProcessAppleEvent(&event);
}

static void failure(const char *text)
{
    Str255 message;
    short button;
    pascal_string(message, text);
    StandardAlert(kAlertStopAlert, message, NULL, NULL, &button);
}

static OSErr finder_event(const FSSpec *file, AEEventClass event_class, AEEventID event_id,
                         Boolean wait)
{
    OSType finder = 'MACS';
    AEAddressDesc target = {typeNull, NULL};
    AppleEvent event = {typeNull, NULL};
    AppleEvent reply = {typeNull, NULL};
    AEDescList list = {typeNull, NULL};
    AliasHandle alias = NULL;
    OSErr err = NewAlias(NULL, file, &alias);
    if (err == noErr) err = AECreateDesc(typeApplSignature, &finder, sizeof finder, &target);
    if (err == noErr) err = AECreateAppleEvent(event_class, event_id,
        &target, kAutoGenerateReturnID, kAnyTransactionID, &event);
    if (err == noErr) err = AECreateList(NULL, 0, false, &list);
    if (err == noErr) {
        HLock((Handle)alias);
        err = AEPutPtr(&list, 0, typeAlias, *alias, GetHandleSize((Handle)alias));
        HUnlock((Handle)alias);
    }
    if (err == noErr) err = AEPutParamDesc(&event, keyDirectObject, &list);
    if (err == noErr) err = AESend(&event, &reply,
        (wait ? kAEWaitReply : kAENoReply) | kAECanSwitchLayer,
        kAENormalPriority, 60 * 15, NULL, NULL);
    if (err == noErr && wait) {
        long result = 0;
        DescType type;
        Size size;
        if (AEGetParamPtr(&reply, keyErrorNumber, typeLongInteger, &type,
                          &result, sizeof result, &size) == noErr)
            err = (OSErr)result;
    }
    AEDisposeDesc(&reply);
    AEDisposeDesc(&list); AEDisposeDesc(&event); AEDisposeDesc(&target);
    if (alias) DisposeHandle((Handle)alias);
    return err;
}

static OSErr finder_open(const FSSpec *file)
{
    return finder_event(file, kCoreEventClass, kAEOpenDocuments, false);
}

static void finder_eject(short volume)
{
    FSSpec root;
    Str255 path;
    pascal_string(path, ":");
    if (FSMakeFSSpec(volume, fsRtDirID, path, &root) == noErr)
        finder_event(&root, kAEFinderSuite, kAEEject, true);
}

static short myst_volume(void)
{
    Str255 name;
    HParamBlockRec pb;
    short index;
    for (index = 1; index < 128; ++index) {
        memset(&pb, 0, sizeof pb);
        pb.volumeParam.ioNamePtr = name;
        pb.volumeParam.ioVolIndex = index;
        if (PBHGetVInfoSync(&pb) != noErr) break;
        if (name[0] == 4 && memcmp(name + 1, "Myst", 4) == 0)
            return pb.volumeParam.ioVRefNum;
    }
    return 0;
}

static Boolean game_process(ProcessSerialNumber *psn)
{
    ProcessInfoRec info;
    memset(psn, 0, sizeof *psn);
    while (GetNextProcess(psn) == noErr) {
        memset(&info, 0, sizeof info);
        info.processInfoLength = sizeof info;
        if (GetProcessInformation(psn, &info) == noErr && info.processSignature == 'MYST')
            return true;
    }
    return false;
}

int main(void)
{
    ProcessSerialNumber self, game;
    ProcessInfoRec info;
    FSSpec app, image, original;
    Str255 name;
    unsigned long deadline;
    short volume;
    Boolean mounted_here = false;
    InitGraf(&qd.thePort); InitFonts(); InitWindows(); InitMenus();
    TEInit(); InitDialogs(NULL); InitCursor();
    AEInstallEventHandler(kCoreEventClass, kAEQuitApplication,
        NewAEEventHandlerUPP(quit_event), 0, false);
    if (game_process(&game)) { SetFrontProcess(&game); return 0; }
    GetCurrentProcess(&self);
    memset(&info, 0, sizeof info);
    info.processInfoLength = sizeof info;
    info.processAppSpec = &app;
    if (GetProcessInformation(&self, &info) != noErr) return 1;
    pascal_string(name, " Myst");
    if (FSMakeFSSpec(app.vRefNum, app.parID, name, &original) != noErr) {
        failure("The original Myst game is missing. Restore the Deluxe game folder."); return 1;
    }
    volume = myst_volume();
    if (!volume) {
        pascal_string(name, "Myst CD.iso");
        if (FSMakeFSSpec(app.vRefNum, app.parID, name, &image) != noErr ||
            finder_open(&image) != noErr) {
            failure("Myst's local disc image could not be opened."); return 1;
        }
        deadline = TickCount() + 60 * 30;
        while (!(volume = myst_volume()) && !quitting && TickCount() < deadline) yield();
        if (!volume) {
            failure("Myst's disc did not mount. Check Virtual CD/DVD-ROM Utility."); return 1;
        }
        mounted_here = true;
    }
    if (finder_open(&original) != noErr) {
        failure("The original Myst game could not be opened.");
        if (mounted_here) finder_eject(volume);
        return 1;
    }
    deadline = TickCount() + 60 * 30;
    while (!game_process(&game) && !quitting && TickCount() < deadline) yield();
    if (game_process(&game)) {
        SetFrontProcess(&game);
        while (game_process(&game) && !quitting) yield();
    }
    /* Never eject media while the original game still owns it. */
    if (mounted_here && !game_process(&game)) finder_eject(volume);
    return 0;
}
