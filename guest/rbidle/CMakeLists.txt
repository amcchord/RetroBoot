# RetroBoot Idle: 68k startup extension (INIT) that stops the 68040 while
# Mac OS is idle (see rbidle.c).  Built with Retro68's 68k toolchain as a flat
# code resource (see build.sh).
cmake_minimum_required(VERSION 3.10)
project(RetroBootIdle C)

if(NOT CMAKE_SYSTEM_NAME MATCHES "^Retro68$")
    message(FATAL_ERROR "Configure with Retro68's 68k toolchain file "
                        "(m68k-apple-macos/cmake/retro68.toolchain.cmake).")
endif()

set(RBIDLE_MIN_RUN 4 CACHE STRING "idle calls in one tick before RetroBoot Idle stops the CPU")
set(CMAKE_C_STANDARD 99)
add_compile_options(-Os -Wall -Wno-multichar -ffunction-sections
    --param=min-pagesize=0          # low-memory globals are real addresses
    -DRBIDLE_MIN_RUN=${RBIDLE_MIN_RUN})

# Step 1: the code as a flat piece of 68k code (no CODE/jump table)
add_executable(RetroBootIdle rbidle.c)
set_target_properties(RetroBootIdle PROPERTIES
    OUTPUT_NAME RetroBootIdle.flt
    LINK_FLAGS "-Wl,--mac-flat -Wl,-gc-sections")

# Step 2: Rez wraps it in 'INIT' 128 (MacBinary output, type INIT, creator RBid)
add_custom_command(
    OUTPUT RetroBootIdle.bin
    COMMAND ${REZ} -I${REZ_INCLUDE_PATH}
                   ${CMAKE_CURRENT_SOURCE_DIR}/rbidle.r
                   -o RetroBootIdle.bin
                   -t INIT -c RBid
    DEPENDS RetroBootIdle rbidle.r)
add_custom_target(RetroBootIdle_INIT ALL DEPENDS RetroBootIdle.bin)
