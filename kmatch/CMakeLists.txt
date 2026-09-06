cmake_minimum_required(VERSION 3.16)
project(kmatch CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
add_compile_options(-Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion)

add_library(kmatch STATIC src/types.cpp src/edges.cpp src/matcher.cpp)
target_include_directories(kmatch PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)

add_executable(kmatch-replay tools/replay.cpp)
target_link_libraries(kmatch-replay kmatch)

enable_testing()
foreach(t test_edges test_matcher test_faults)
  add_executable(${t} tests/${t}.cpp tests/fakecam.cpp)
  target_link_libraries(${t} kmatch)
  add_test(NAME ${t} COMMAND ${t})
endforeach()
