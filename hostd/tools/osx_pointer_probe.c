/* Original native OS X diagnostic. No guest driver code is copied here.
 * Read current pointer conversion; --absolute requests native absolute mode
 * for this diagnostic session. It does not install a driver or persist prefs.
 */
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/hidsystem/IOHIDLib.h>
#include <IOKit/hidsystem/IOHIDShared.h>
#include <stdio.h>
#include <string.h>

static void devices(void)
{
    io_iterator_t iterator = 0;
    io_registry_entry_t entry;
    if (IOServiceGetMatchingServices(kIOMasterPortDefault,
                                    IOServiceMatching("IOHIPointing"), &iterator) != KERN_SUCCESS)
        return;
    while ((entry = IOIteratorNext(iterator))) {
        io_name_t name;
        CFTypeRef value;
        IORegistryEntryGetName(entry, name);
        printf("pointer=%s\n", name);
        value = IORegistryEntryCreateCFProperty(entry, CFSTR("HIDPointerConvertAbsolute"),
                                                kCFAllocatorDefault, 0);
        if (value) {
            CFShow(value);
            CFRelease(value);
        } else {
            puts("conversion property absent");
        }
        IOObjectRelease(entry);
    }
    IOObjectRelease(iterator);
}

int main(int argc, char **argv)
{
    io_service_t service;
    io_connect_t connection = 0;
    kern_return_t result;
    UInt32 convert = 0;
    puts("before:");
    devices();
    if (argc != 2 || strcmp(argv[1], "--absolute"))
        return 0;
    service = IOServiceGetMatchingService(kIOMasterPortDefault, IOServiceMatching("IOHIDSystem"));
    if (!service)
        return 2;
    result = IOServiceOpen(service, mach_task_self(), kIOHIDParamConnectType, &connection);
    IOObjectRelease(service);
    if (result != KERN_SUCCESS) {
        printf("open=0x%x\n", result);
        return 3;
    }
    result = IOHIDSetParameter(connection, CFSTR("HIDPointerConvertAbsolute"), &convert, sizeof(convert));
    printf("set absolute=0x%x\n", result);
    IOServiceClose(connection);
    puts("after:");
    devices();
    return result == KERN_SUCCESS ? 0 : 4;
}
