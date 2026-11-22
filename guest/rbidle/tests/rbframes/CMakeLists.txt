# RBFrames: lab test application for RetroBoot Idle (not shipped).  Measures
# how many "frames" a game-like 68k loop gets per second when it polls for
# events once per frame, with and without RetroBoot Idle, then writes
# "RBFrames Results" next to itself and shuts the Mac down.
cmake_minimum_required(VERSION 3.10)
project(RBFrames C)
set(CMAKE_C_STANDARD 99)
add_compile_options(-O2 -Wall -Wno-multichar)
add_application(RBFrames TYPE APPL CREATOR RBfr FILES rbframes.c rbframes.r)
