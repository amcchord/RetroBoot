# RetroBoot Display: a background-only classic Mac OS PowerPC (CFM) application
# that puts Mac OS's screen at the size hostd chose for the emulated ATI Radeon
# (ARCHITECTURE 4.9).  Built with Retro68's PowerPC toolchain (see build.sh).
cmake_minimum_required(VERSION 3.10)
project(RetroBootDisplay C)

if(NOT CMAKE_SYSTEM_NAME MATCHES "^RetroPPC$")
    message(FATAL_ERROR "Configure with Retro68's PowerPC toolchain file "
                        "(powerpc-apple-macos/cmake/retroppc.toolchain.cmake).")
endif()

set(CMAKE_C_STANDARD 99)
add_compile_options(-O2 -Wall -Wno-multichar -ffunction-sections)

add_application(RetroBootDisplay
    TYPE appe CREATOR RBds
    FILES rbdisplay.c rbdisplay.r)
target_link_libraries(RetroBootDisplay DisplayLib NameRegistryLib)
set_target_properties(RetroBootDisplay PROPERTIES LINK_FLAGS "-Wl,-gc-sections")
