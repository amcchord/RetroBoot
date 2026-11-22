#include "MacTypes.r"
#include "Dialogs.r"
#include "Processes.r"
resource 'ALRT' (128) {
    {60, 60, 180, 460}, 128,
    {OK, visible, sound1, OK, visible, sound1,
     OK, visible, sound1, OK, visible, sound1}, centerMainScreen
};
resource 'DITL' (128) { {
    {85, 310, 105, 380}, Button {enabled, "OK"},
    {12, 70, 74, 384}, StaticText {disabled, "^0"}
} };
resource 'SIZE' (-1) {
    reserved, ignoreSuspendResumeEvents, reserved, cannotBackground,
    doesActivateOnFGSwitch, backgroundAndForeground, dontGetFrontClicks,
    ignoreChildDiedEvents, is32BitCompatible, notHighLevelEventAware,
    onlyLocalHLEvents, notStationeryAware, dontUseTextEditServices,
    reserved, reserved, reserved, 256 * 1024, 128 * 1024
};
