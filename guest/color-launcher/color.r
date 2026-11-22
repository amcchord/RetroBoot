#include "Processes.r"
#include "MacTypes.r"
#include "Dialogs.r"
resource 'ALRT' (128, purgeable) {
    {60, 60, 180, 460}, 128,
    {OK, visible, sound1, OK, visible, sound1,
     OK, visible, sound1, OK, visible, sound1},
    centerMainScreen
};
resource 'DITL' (128, purgeable) {
    {
        {85, 310, 105, 380}, Button {enabled, "OK"},
        {12, 70, 74, 384}, StaticText {disabled, "^0"}
    }
};
resource 'vers' (1) {
    0x01, 0x00, release, 0x00, verUS,
    "1.0", "1.0, RetroBoot: original games with automatic colors"
};
resource 'SIZE' (-1) {
    reserved, ignoreSuspendResumeEvents, reserved, canBackground,
    doesActivateOnFGSwitch, backgroundAndForeground, dontGetFrontClicks,
    ignoreChildDiedEvents, is32BitCompatible, isHighLevelEventAware,
    onlyLocalHLEvents, notStationeryAware, dontUseTextEditServices,
    reserved, reserved, reserved, 512 * 1024, 256 * 1024
};
