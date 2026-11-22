# RetroBoot Display (startup extension): the 68k INIT variant of RetroBoot
# Display, which sets the Mac OS screen size hostd chose for the emulated ATI
# Radeon while the extensions load (ARCHITECTURE 4.9).  Built with Retro68's
# 68k toolchain as a flat code resource (see build.sh).
cmake_minimum_required(VERSION 3.10)
project(RetroBootDisplayInit C)

if(NOT CMAKE_SYSTEM_NAME MATCHES "^Retro68$")
    message(FATAL_ERROR "Configure with Retro68's 68k toolchain file "
                        "(m68k-apple-macos/cmake/retro68.toolchain.cmake).")
endif()

set(CMAKE_C_STANDARD 99)
add_compile_options(-Os -Wall -Wno-multichar -ffunction-sections
    --param=min-pagesize=0)          # low-memory globals (0x928...) are real addresses

# Step 1: the code as a flat piece of 68k code (no CODE/jump table)
add_executable(RetroBootDisplayInit rbdisplayinit.c)
set_target_properties(RetroBootDisplayInit PROPERTIES
    OUTPUT_NAME RetroBootDisplayInit.flt
    LINK_FLAGS "-Wl,--mac-flat -Wl,-gc-sections")

# Step 2: Rez wraps it in 'INIT' 128 (MacBinary output, type INIT)
add_custom_command(
    OUTPUT RetroBootDisplayInit.bin
    COMMAND ${REZ} -I${REZ_INCLUDE_PATH}
                   ${CMAKE_CURRENT_SOURCE_DIR}/rbdisplayinit.r
                   -o RetroBootDisplayInit.bin
                   -t INIT -c RBds
    DEPENDS RetroBootDisplayInit rbdisplayinit.r)
add_custom_target(RetroBootDisplayInit_INIT ALL DEPENDS RetroBootDisplayInit.bin)
