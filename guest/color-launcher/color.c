/* RetroBoot original source, MIT. Original game code remains unchanged. */
#include <AppleEvents.h>
#include <Aliases.h>
#include <Dialogs.h>
#include <Events.h>
#include <Files.h>
#include <Palettes.h>
#include <Processes.h>
#include <Quickdraw.h>
#include <Resources.h>
#include <string.h>

static Boolean quitting;

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
    size_t n = strlen(text);
    if (n > 255) n = 255;
    message[0] = (unsigned char)n;
    memcpy(message + 1, text, n);
#if defined(__m68k__)
    ParamText(message, NULL, NULL, NULL);
    StopAlert(128, NULL);
#else
    {
        short button;
        StandardAlert(kAlertStopAlert, message, NULL, NULL, &button);
    }
#endif
}

static Boolean same_file(const FSSpec *a, const FSSpec *b)
{
    return a->vRefNum == b->vRefNum && a->parID == b->parID &&
        a->name[0] == b->name[0] &&
        memcmp(a->name + 1, b->name + 1, a->name[0]) == 0;
}

static Boolean game_process(const FSSpec *app, ProcessSerialNumber *psn)
{
    ProcessInfoRec info;
    FSSpec file;
    memset(psn, 0, sizeof *psn);
    while (GetNextProcess(psn) == noErr) {
        memset(&info, 0, sizeof info);
        info.processInfoLength = sizeof info;
        info.processAppSpec = &file;
        if (GetProcessInformation(psn, &info) == noErr && same_file(&file, app))
            return true;
    }
    return false;
}

static OSErr finder_open(const FSSpec *file)
{
    OSType finder = 'MACS';
    AEAddressDesc target = {typeNull, NULL};
    AppleEvent event = {typeNull, NULL}, reply = {typeNull, NULL};
    AEDescList list = {typeNull, NULL};
    AliasHandle alias = NULL;
    OSErr err = NewAlias(NULL, file, &alias);
    if (err == noErr) err = AECreateDesc(typeApplSignature, &finder, sizeof finder, &target);
    if (err == noErr) err = AECreateAppleEvent(kCoreEventClass, kAEOpenDocuments,
        &target, kAutoGenerateReturnID, kAnyTransactionID, &event);
    if (err == noErr) err = AECreateList(NULL, 0, false, &list);
    if (err == noErr) {
        HLock((Handle)alias);
        err = AEPutPtr(&list, 0, typeAlias, *alias, GetHandleSize((Handle)alias));
        HUnlock((Handle)alias);
    }
    if (err == noErr) err = AEPutParamDesc(&event, keyDirectObject, &list);
    if (err == noErr) err = AESend(&event, &reply, kAENoReply | kAECanSwitchLayer,
        kAENormalPriority, 60 * 15, NULL, NULL);
    AEDisposeDesc(&reply); AEDisposeDesc(&list);
    AEDisposeDesc(&event); AEDisposeDesc(&target);
    if (alias) DisposeHandle((Handle)alias);
    return err;
}

int main(void)
{
    ProcessSerialNumber self, game;
    ProcessInfoRec info;
    FSSpec app, original;
    Str255 name;
    Handle config;
    GDHandle device;
    short desired, prior;
    Boolean changed = false;
    unsigned long deadline;
    InitGraf(&qd.thePort); InitFonts(); InitWindows(); InitMenus();
    TEInit(); InitDialogs(NULL); InitCursor();
    AEInstallEventHandler(kCoreEventClass, kAEQuitApplication,
        NewAEEventHandlerUPP(quit_event), 0, false);
    config = Get1Resource('RBcf', 128);
    if (!config || GetHandleSize(config) < 4) {
        failure("This game link has no valid setup. Restore the game folder.");
        return 1;
    }
    HLock(config);
    desired = (unsigned char)(*config)[1];
    name[0] = (unsigned char)(*config)[2];
    if ((*config)[0] != 1 || (desired != 1 && desired != 4 && desired != 8 && desired != 16 && desired != 32) ||
        !name[0] || name[0] > 31 || GetHandleSize(config) != name[0] + 3) {
        HUnlock(config);
        failure("This game link has an invalid setup. Restore the game folder.");
        return 1;
    }
    memcpy(name + 1, *config + 3, name[0]);
    HUnlock(config);
    GetCurrentProcess(&self);
    memset(&info, 0, sizeof info);
    info.processInfoLength = sizeof info;
    info.processAppSpec = &app;
    if (GetProcessInformation(&self, &info) != noErr ||
        FSMakeFSSpec(app.vRefNum, app.parID, name, &original) != noErr) {
        failure("The original game is missing. Keep this game link in its original folder.");
        return 1;
    }
    if (game_process(&original, &game)) { SetFrontProcess(&game); return 0; }
    device = GetMainDevice();
    prior = (**(**device).gdPMap).pixelSize;
    if (prior != desired) {
        if (!HasDepth(device, desired, 0, 0) || SetDepth(device, desired, 0, 0) != noErr) {
            failure("The selected display cannot use this game's colors. Choose another screen mode.");
            return 1;
        }
        changed = true;
    }
    if (finder_open(&original) != noErr) {
        if (changed) SetDepth(GetMainDevice(), prior, 0, 0);
        failure("The original game could not be opened.");
        return 1;
    }
    deadline = TickCount() + 60 * 30;
    while (!game_process(&original, &game) && !quitting && TickCount() < deadline) yield();
    if (game_process(&original, &game)) {
        SetFrontProcess(&game);
        while (game_process(&original, &game) && !quitting) yield();
    } else if (!quitting) {
        failure("The original game did not start. Check its game folder.");
    }
    /* A system quit can reach us before the game: never change its active screen. */
    if (changed && !game_process(&original, &game) &&
        (**(**GetMainDevice()).gdPMap).pixelSize == desired)
        SetDepth(GetMainDevice(), prior, 0, 0);
    return 0;
}
