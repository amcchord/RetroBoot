#include "Processes.r"
#include "MacTypes.r"
resource 'vers' (1) {
    0x01, 0x00, release, 0x00, verUS,
    "1.0", "1.0, RetroBoot: mount Myst media only when playing"
};
resource 'SIZE' (-1) {
    reserved, ignoreSuspendResumeEvents, reserved, canBackground,
    doesActivateOnFGSwitch, backgroundAndForeground, dontGetFrontClicks,
    ignoreChildDiedEvents, is32BitCompatible, isHighLevelEventAware,
    onlyLocalHLEvents, notStationeryAware, dontUseTextEditServices,
    reserved, reserved, reserved, 1024 * 1024, 512 * 1024
};
